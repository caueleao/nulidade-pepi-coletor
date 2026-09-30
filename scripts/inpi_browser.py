"""Playwright-based browser session for INPI pePI portal.

Flow per patent process:
  1. Anonymous login (POST empty T_Login/T_Senha form)
  2. Fill NumPedido + submit → result page
  3. page.goto() to detail URL (click times out due to JS navigation)
  4. Parse detail HTML: extract metadata (72, 74, 30) and salvaDocumento img
  5. Find img matching target RPI + codigo → NumeroID (SHA-256 hash attribute)
  6. reCAPTCHA v2: manual (user solves in browser) or 2captcha
  7. Validate: GET ImagemDocumentoPdfController?action=validaCaptcha&NumID=…&captcha=…
  8. Download: GET ImagemDocumentoPdfController?CodDiretoria=200&NumeroID=…&codPedido=…
"""
from __future__ import annotations
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

import config as cfg

log = logging.getLogger(__name__)

_HOST = "https://busca.inpi.gov.br"
_LOGIN_URL = f"{_HOST}/pePI/jsp/patentes/PatenteSearchBasico.jsp"
_SEARCH_BASE = cfg.BUSCA_BASE_URL
_VALIDATE_URL = f"{_HOST}/pePI/servlet/ImagemDocumentoPdfController"
_DOWNLOAD_URL = f"{_HOST}/pePI/servlet/ImagemDocumentoPdfController"
# reCAPTCHA v2 site key (standard, not Enterprise)
_RECAPTCHA_SITEKEY = "6LfhwSAaAAAAANyx2xt8Ikk-YkQ3PGeAVhCfF3i2"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

class BrowserSession:
    """Context manager wrapping a single Playwright browser for the whole run."""

    def __init__(self, headless: bool | None = None, captcha_mode: str | None = None,
                 proxy: dict | None = None, storage_state_path: Path | None = None):
        self._headless = headless if headless is not None else cfg.BROWSER_HEADLESS
        self._captcha_mode = captcha_mode or cfg.CAPTCHA_MODE
        self._proxy = proxy
        self._storage_state_path = storage_state_path or cfg.BROWSER_STORAGE_STATE
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._logged_in = False
        self._captcha_count = 0
        # Cache de aceite da "Declaração de Finalidade" por cod_pedido — necessário
        # para acessar a tabela "Serviços" (petições 207/281). Cookie persiste pela
        # sessão; evita re-aceite redundante na mesma BrowserSession.
        self._declaracao_aceita_for: set[str] = set()

    def __enter__(self) -> "BrowserSession":
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()

        # Args para reduzir fingerprint de bot detection
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--no-sandbox",
        ]
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            slow_mo=cfg.BROWSER_SLOW_MO,
            args=launch_args,
        )

        # Use a real browser User-Agent, not our custom one (INPI may flag it)
        REAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36")
        ctx_kwargs: dict[str, Any] = {
            "user_agent": REAL_UA,
            "accept_downloads": True,
            "viewport": {"width": 1280, "height": 800},
            "locale": "pt-BR",
            "timezone_id": "America/Sao_Paulo",
        }
        if self._proxy:
            ctx_kwargs["proxy"] = self._proxy
            log.info("Proxy ativo: %s (user=%s)", self._proxy.get("server", "?"),
                     (self._proxy.get("username") or "?")[:30])
        state = self._storage_state_path
        if state.exists():
            ctx_kwargs["storage_state"] = str(state)
            log.info("Sessão pePI carregada de %s", state)

        self._context = self._browser.new_context(**ctx_kwargs)
        self._context.set_default_timeout(cfg.BROWSER_NAV_TIMEOUT)
        self._page = self._context.new_page()

        # Apply playwright-stealth to reduce bot fingerprinting
        try:
            from playwright_stealth import Stealth
            Stealth().apply_stealth_sync(self._page)
            log.info("Stealth aplicado ao browser")
        except ImportError:
            log.warning("playwright-stealth não instalado — fingerprint não mascarada")
        except Exception as e:
            log.warning("Falha ao aplicar stealth: %s", e)

        return self

    def __exit__(self, *_) -> None:
        try:
            self._storage_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._context.storage_state(path=str(self._storage_state_path))
            log.info("Sessão pePI salva em %s", self._storage_state_path)
        except Exception as e:
            log.warning("Falha ao salvar sessão: %s", e)
        finally:
            try:
                self._page.close()
                self._context.close()
                self._browser.close()
                self._pw.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # High-level operations
    # ------------------------------------------------------------------

    def enrich_and_fetch_pdf(self, numero: str, rpi_numero: int, codigo: str) -> dict:
        """Navigate to process detail, extract metadata, download matching despacho PDF.

        Returns dict with:
          - metadata: bib data
          - pdf_bytes: PDF do despacho atual (alvo)
          - pdf_url: URL do PDF
          - publicacoes: lista de TODAS as publicações na tabela (para cascata histórica)
          - cod_pedido: usado pra outros downloads na mesma sessão
        """
        self._ensure_logged_in()
        detail_url, cod_pedido = self._search_process(numero)
        if not detail_url:
            return {"metadata": {}, "pdf_bytes": None, "pdf_url": None,
                    "publicacoes": [], "cod_pedido": None}

        detail_timeout = max(cfg.BROWSER_NAV_TIMEOUT, 120_000)
        try:
            self._page.goto(detail_url, wait_until="domcontentloaded", timeout=detail_timeout)
        except Exception:
            try:
                self._page.goto(detail_url, wait_until="commit", timeout=detail_timeout)
                self._page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception:
                pass

        # Aguarda página estabilizar antes de ler conteúdo (evita race condition
        # "Page.content: Unable to retrieve content because the page is navigating").
        # pePI faz redirects/navigates JS após domcontentloaded em algumas patentes.
        try:
            self._page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        # Retry tolerante a "navigating and changing the content"
        html = ""
        for attempt in range(3):
            try:
                html = self._page.content()
                break
            except Exception as e:
                if "navigating" in str(e).lower() or "changing" in str(e).lower():
                    log.debug("page.content() retry %d/3 (page navigating)", attempt + 1)
                    time.sleep(2)
                    continue
                log.warning("page.content() falhou: %s", e)
                break

        metadata = _extract_metadata(html)
        img_info = _find_salva_documento_img(html, rpi_numero, codigo, cod_pedido)
        publicacoes = _extract_publicacoes_table(html, cod_pedido)

        pdf_bytes = None
        pdf_url = None
        if img_info:
            numero_id = img_info["id"]
            effective_cod_pedido = img_info.get("codPedido") or cod_pedido
            pdf_bytes, pdf_url = self._download_pdf_with_captcha(numero_id, effective_cod_pedido)
        else:
            log.warning("Nenhum img.salvaDocumento encontrado para %s rpi=%d cod=%s", numero, rpi_numero, codigo)

        # Petições da tabela "Serviços" — só vale a pena para gatilho 9.1.
        # Requer aceitar a Declaração de Finalidade (1x por cod_pedido na sessão).
        # Re-navega para detail_url antes do aceite porque _download_pdf_with_captcha
        # modificou o DOM (modal reCAPTCHA injetada) e o link sumiu.
        servicos: list[dict] = []
        if codigo == "9.1" and cod_pedido:
            try:
                if self._aceitar_declaracao(cod_pedido, detail_url):
                    html_pos = self._page.content()
                    servicos = _extract_servicos_table(html_pos, cod_pedido)
                    log.info("Serviços extraídos para %s: %d linhas", numero, len(servicos))
            except Exception as e:
                log.warning("Falha ao extrair Serviços de %s: %s", numero, e)

        return {
            "metadata": metadata,
            "pdf_bytes": pdf_bytes,
            "pdf_url": pdf_url,
            "publicacoes": publicacoes,
            "servicos": servicos,
            "cod_pedido": cod_pedido,
        }

    def fetch_pdf_by_img_id(self, numero_id: str, cod_pedido: str | None) -> bytes | None:
        """Baixa o PDF de uma img.salvaDocumento específica (sem renavegar a página).

        Reusa o flow de CAPTCHA + download. Útil para baixar despachos históricos
        após o download principal do 9.1/9.2.
        """
        pdf_bytes, _ = self._download_pdf_with_captcha(numero_id, cod_pedido)
        return pdf_bytes

    # ------------------------------------------------------------------
    # Declaração de Finalidade (para tabela Serviços / petições 207/281)
    # ------------------------------------------------------------------

    _LINK_PETICOES_TEXTS = (
        "Clique aqui para ter acesso às petições",
        "Clique aqui para ter acesso as petições",
        "acesso às petições do processo",
    )
    # Hipótese 3 = "Pesquisa para Fins Profissionais ou Acadêmicos"
    _CODIGO_HIPOTESE_PESQUISA = "3"

    def _aceitar_declaracao(self, cod_pedido: str, detail_url: str | None = None) -> bool:
        """Aceita a Declaração de Finalidade para acessar a tabela 'Serviços'.

        Fluxo (espelha AgenticAPI/inpi_petitions.py:aceitar_declaracao):
        1. Re-navega para detail_url (DOM pode ter sido modificado por modal reCAPTCHA);
        2. No-op se a página atual já mostra petições (códigos 3 dígitos);
        3. Click no link "Clique aqui para ter acesso às petições" → popup;
        4. No popup: select codigoHipotese=3, check aceite, click Enviar;
        5. page.goto(current_url) para refrescar com cookie de aceite ativo;
        6. Re-valida que Serviços apareceram.

        Cache `self._declaracao_aceita_for` evita re-aceite por cod_pedido.
        Retorna True se petições agora estão acessíveis na página atual.
        """
        if not cod_pedido:
            log.info("Aceite Declaração: skip — cod_pedido vazio")
            return False
        if cod_pedido in self._declaracao_aceita_for:
            log.info("Aceite Declaração: cache hit p/ %s", cod_pedido)
            return True

        log.info("Aceite Declaração: iniciando p/ cod_pedido=%s", cod_pedido)

        # 0) Re-navega para detail_url para garantir DOM limpo (sem modal reCAPTCHA)
        if detail_url:
            try:
                self._page.goto(detail_url, wait_until="domcontentloaded", timeout=30_000)
                try:
                    self._page.wait_for_load_state("networkidle", timeout=8_000)
                except Exception:
                    pass
            except Exception as e:
                log.warning("Aceite Declaração: re-navegação falhou p/ %s: %s", cod_pedido, e)

        try:
            html = self._page.content()
        except Exception:
            html = ""
        # Já visível?
        if _servicos_visiveis(html):
            log.info("Aceite Declaração: tabela Serviços já visível p/ %s (sem aceite necessário)", cod_pedido)
            self._declaracao_aceita_for.add(cod_pedido)
            return True

        # 1) Click no link → captura popup. Tenta múltiplos seletores Playwright.
        link_selectors = (
            "a:has-text('Clique aqui para ter acesso')",
            "a:has-text('acesso às petições')",
            "a:has-text('acesso as petições')",
        )
        try:
            with self._page.expect_popup(timeout=10_000) as popup_info:
                clicked = False
                for sel in link_selectors:
                    try:
                        loc = self._page.locator(sel).first
                        if loc.count() > 0:
                            loc.click(timeout=4_000)
                            clicked = True
                            log.info("Aceite Declaração: click ok via selector '%s'", sel)
                            break
                    except Exception as e:
                        log.debug("Selector '%s' falhou: %s", sel, e)
                if not clicked:
                    # Fallback: text-based (igual antes)
                    clicked = self._click_first_match(self._LINK_PETICOES_TEXTS)
                if not clicked:
                    # Última tentativa: dump HTML pra diagnóstico
                    log.warning("Aceite Declaração: link 'acesso às petições' não encontrado em %s", cod_pedido)
                    try:
                        from pathlib import Path as _P
                        dbg = _P('/tmp/inpi_no_link.html')
                        dbg.write_text(html or self._page.content(), encoding='utf-8')
                        log.warning("Debug: HTML salvo em %s", dbg)
                    except Exception:
                        pass
                    return False
            popup = popup_info.value
            log.info("Aceite Declaração: popup aberto p/ %s", cod_pedido)
        except Exception as e:
            log.warning("Aceite Declaração: popup não abriu p/ %s: %s", cod_pedido, e)
            return False

        try:
            popup.wait_for_load_state("domcontentloaded", timeout=15_000)
            # 2) select hipótese
            try:
                popup.select_option(
                    "select[name='codigoHipotese']",
                    self._CODIGO_HIPOTESE_PESQUISA,
                    timeout=5_000,
                )
            except Exception as e:
                log.warning("select[codigoHipotese] falhou p/ %s: %s", cod_pedido, e)
                return False
            # 3) marcar aceite
            try:
                popup.check("input[name='aceite']", timeout=5_000)
            except Exception as e:
                log.warning("checkbox aceite falhou p/ %s: %s", cod_pedido, e)
                return False
            # 4) submit
            try:
                popup.click("input[type='submit'][value='Enviar']", timeout=5_000)
            except Exception as e:
                log.warning("submit Declaração falhou p/ %s: %s", cod_pedido, e)
                return False
        finally:
            try:
                if not popup.is_closed():
                    popup.close()
            except Exception:
                pass

        # 5) Re-navega para forçar fetch com cookie de aceite (reload causa
        # ERR_ABORTED quando o popup ainda está fazendo POST).
        time.sleep(1.0)  # respira p/ POST do popup completar no servidor
        current_url = self._page.url
        try:
            self._page.goto(current_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:
            log.warning("Reload pós-aceite falhou p/ %s: %s", cod_pedido, e)
            return False

        try:
            self._page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        try:
            html2 = self._page.content()
        except Exception:
            html2 = ""
        # Debug: salva HTML pós-aceite quando solicitado via env
        if os.environ.get("DEBUG_DUMP_SERVICOS"):
            try:
                from pathlib import Path as _P
                dbg = _P(f'/tmp/inpi_servicos_{cod_pedido}.html')
                dbg.write_text(html2, encoding='utf-8')
                log.info("DEBUG: HTML pós-aceite salvo em %s (%d bytes)", dbg, len(html2))
            except Exception:
                pass
        if _servicos_visiveis(html2):
            self._declaracao_aceita_for.add(cod_pedido)
            log.info("Declaração aceita p/ %s — tabela Serviços com petições visíveis", cod_pedido)
            return True
        # Mesmo sem petições visíveis, aceite teve sucesso se o link sumiu.
        # Sinal robusto: a frase "Clique aqui para ter acesso" só aparece pré-aceite.
        link_ainda_presente = any(
            txt in html2 for txt in ("Clique aqui para ter acesso", "acesso às petições", "acesso as petições")
        )
        if not link_ainda_presente:
            self._declaracao_aceita_for.add(cod_pedido)
            log.info("Declaração aceita p/ %s — link sumiu (Serviços vazia: sem petições 207/281 nessa patente)", cod_pedido)
            return True
        log.warning("Aceite Declaração: link ainda presente após submit p/ %s — aceite pode ter falhado", cod_pedido)
        return False

    def _click_first_match(self, candidates: tuple[str, ...]) -> bool:
        """Clica no primeiro elemento (link/botão) cujo texto contém qualquer
        dos `candidates`. Case-insensitive. Retorna True se algum click teve sucesso.
        """
        for txt in candidates:
            try:
                loc = self._page.get_by_text(txt, exact=False)
                if loc.count() > 0:
                    loc.first.click(timeout=5_000)
                    return True
            except Exception:
                continue
        return False

    def enrich_only(self, numero: str) -> dict:
        """Fetch metadata from detail page without downloading PDF."""
        self._ensure_logged_in()
        detail_url, _ = self._search_process(numero)
        if not detail_url:
            return {}
        self._page.goto(detail_url, wait_until="domcontentloaded", timeout=cfg.BROWSER_NAV_TIMEOUT)
        return _extract_metadata(self._page.content())

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def _ensure_logged_in(self) -> None:
        if self._logged_in:
            return
        # Login autenticado quando PEPI_USER/PEPI_PASS estão configurados (necessário
        # para acessar tabela "Serviços" / petições 207/281). Caso contrário, faz
        # login anônimo (campos vazios) — funciona para download de Publicações.
        user = cfg.PEPI_USER or ""
        pwd = cfg.PEPI_PASS or ""
        modo = "autenticado" if user else "anônimo"
        log.info("Fazendo login %s no pePI…", modo)
        self._page.goto(_LOGIN_URL, wait_until="domcontentloaded")

        if self._page.query_selector("input[name='T_Login']"):
            self._page.fill("input[name='T_Login']", user)
            self._page.fill("input[name='T_Senha']", pwd)
            self._page.click("input[type='submit']")
            self._page.wait_for_load_state("domcontentloaded")
            # After POST we're at LoginController; navigate back to the search form
            self._page.goto(_LOGIN_URL, wait_until="domcontentloaded")
            try:
                self._page.wait_for_selector("input[name='NumPedido']", timeout=8000)
                log.info("Login %s concluído", modo)
            except Exception:
                log.warning("Página de busca não confirmada após login — continuando mesmo assim")
        else:
            log.info("Sessão existente — login não necessário")

        self._logged_in = True

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _search_process(self, numero: str) -> tuple[str | None, str | None]:
        """Search for the process. Returns (detail_url, cod_pedido) or (None, None)."""
        search_num = re.sub(r"\s+[A-Z]\d+$", "", numero).strip()
        log.debug("Buscando: %s", search_num)

        # Up to 2 attempts: if first fails, force re-login and try again
        for attempt in range(2):
            try:
                self._page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
                self._page.wait_for_selector("input[name='NumPedido']", timeout=15_000)
                self._page.fill("input[name='NumPedido']", search_num, timeout=10_000)
                self._page.click("input[name='botao']")
                self._page.wait_for_load_state("domcontentloaded")
                break
            except Exception as e:
                log.warning("Tentativa %d: falha no formulário de busca para %s: %s", attempt + 1, search_num, e)
                if attempt == 0:
                    self._logged_in = False
                    try:
                        self._ensure_logged_in()
                    except Exception as e2:
                        log.warning("Falha ao re-logar: %s", e2)
                else:
                    return None, None

        soup = BeautifulSoup(self._page.content(), "html.parser")
        detail_a = soup.find("a", href=re.compile(r"Action=detail"))
        if not detail_a:
            log.warning("Processo %s não encontrado no pePI", search_num)
            return None, None

        href = detail_a["href"]
        full_url = href if href.startswith("http") else _HOST + href

        # Extract CodPedido from URL for later use in PDF download
        cod_pedido_match = re.search(r"CodPedido=(\d+)", full_url)
        cod_pedido = cod_pedido_match.group(1) if cod_pedido_match else None

        log.debug("Detalhe: %s (CodPedido=%s)", full_url[:100], cod_pedido)
        return full_url, cod_pedido

    # ------------------------------------------------------------------
    # PDF download
    # ------------------------------------------------------------------

    def _download_pdf_with_captcha(self, numero_id: str, cod_pedido: str | None) -> tuple[bytes | None, str | None]:
        """Download the PDF for the given NumeroID, handling CAPTCHA. Returns (pdf_bytes, pdf_url)."""
        if self._captcha_count >= cfg.CAPTCHA_MAX_PER_RUN:
            raise RuntimeError(f"Disjuntor: {self._captcha_count} CAPTCHAs neste run (limite {cfg.CAPTCHA_MAX_PER_RUN})")

        if self._captcha_mode == "manual":
            return self._manual_browser_download(numero_id, cod_pedido)
        else:
            return self._auto_api_download(numero_id, cod_pedido)

    # ------------------------------------------------------------------
    # Manual mode: click icon in headed browser, user solves CAPTCHA,
    # intercept the PDF HTTP response automatically (no ENTER needed)
    # ------------------------------------------------------------------

    def _manual_browser_download(self, numero_id: str, cod_pedido: str | None) -> tuple[bytes | None, str | None]:
        """Abre o modal do CAPTCHA. Usuário resolve apenas o checkbox.
        Código extrai o token via grecaptcha.getResponse() e baixa o PDF via API
        — sem depender do browser fazer o download."""

        # Click the PDF icon to open the CAPTCHA modal
        try:
            self._page.click(f"img.salvaDocumento[id='{numero_id}']", force=True, timeout=5000)
            time.sleep(1.0)
            log.info("Ícone PDF clicado (NumeroID=%s…)", numero_id[:20])
        except Exception as e:
            log.warning("Click com seletor falhou (%s) — tentando evaluate", e)
            try:
                self._page.evaluate(f"document.getElementById('{numero_id}').click()")
                time.sleep(1.0)
                log.info("Ícone PDF clicado via evaluate")
            except Exception as e2:
                log.error("Falha ao clicar no ícone PDF: %s", e2)
                return None, None

        print(
            f"\n>>> reCAPTCHA aberto no browser."
            f"\n    Marque APENAS o checkbox 'Não sou um robô' (e qualquer desafio que aparecer)."
            f"\n    NÃO clique em 'Download' — o código fará isso automaticamente."
            f"\n    Aguardando token… (timeout: {cfg.CAPTCHA_TIMEOUT}s)\n",
            flush=True,
        )

        # Poll grecaptcha.getResponse() until token is available
        token: str | None = None
        deadline = time.monotonic() + cfg.CAPTCHA_TIMEOUT
        while time.monotonic() < deadline:
            try:
                # Try to read the token from the global grecaptcha API
                resp = self._page.evaluate(
                    "() => { try { return (typeof grecaptcha !== 'undefined' && grecaptcha.getResponse) ? grecaptcha.getResponse() : ''; } catch(e) { return ''; } }"
                )
                if resp and len(resp) > 30:
                    token = resp
                    log.info("Token reCAPTCHA capturado (%d caracteres)", len(token))
                    break
            except Exception as e:
                log.debug("evaluate getResponse error: %s", e)
            time.sleep(1.0)

        if not token:
            log.warning("Timeout aguardando token reCAPTCHA (NumeroID=%s…)", numero_id[:20])
            return None, None

        # Step 1: validate captcha via API
        try:
            val_resp = self._page.request.get(
                _VALIDATE_URL,
                params={"action": "validaCaptcha", "NumID": numero_id, "captcha": token},
                timeout=30_000,
            )
            try:
                val_json = val_resp.json()
            except Exception:
                val_text = val_resp.text()
                log.error("validaCaptcha não retornou JSON: status=%d body=%s", val_resp.status, val_text[:200])
                return None, None
            if val_json.get("category") != "success":
                log.error("CAPTCHA inválido: %s", val_json.get("message", str(val_json)))
                return None, None
            log.debug("CAPTCHA validado pela API: %s", val_json.get("message", ""))
        except Exception as e:
            log.error("Falha na validação de CAPTCHA: %s", e)
            return None, None

        self._captcha_count += 1

        # Step 2: download PDF via API
        params: dict[str, str] = {
            "CodDiretoria": "200",
            "NumeroID": numero_id,
            "certificado": "",
            "numeroProcesso": "",
            "ipasDoc": "",
        }
        if cod_pedido:
            params["codPedido"] = cod_pedido

        try:
            pdf_resp = self._page.request.get(_DOWNLOAD_URL, params=params, timeout=120_000)
            body = pdf_resp.body()
            if len(body) > 1000 and body[:4] == b"%PDF":
                log.info("PDF capturado via API (%d bytes)", len(body))
                return body, pdf_resp.url
            log.warning("Resposta não é PDF: status=%d primeiros bytes=%s", pdf_resp.status, body[:30])
        except Exception as e:
            log.error("Falha no download do PDF: %s", e)

        return None, None

    # ------------------------------------------------------------------
    # Automated mode: token from 2captcha/anticaptcha, direct API calls
    # ------------------------------------------------------------------

    # O INPI rejeita uma fração alta dos tokens ("Dados incorretos, tente
    # novamente!") mesmo quando o solver resolveu corretamente — 12-40% de
    # aceite por token, medido tanto no IP local quanto nos runners do CI (ou
    # seja, não é bloqueio de IP). Como a recusa é do lado do INPI, a única
    # saída é resolver um token NOVO e revalidar: reenviar o mesmo é sempre
    # recusado. Ajustável em [captcha] max_tentativas_documento.
    @property
    def _CAPTCHA_MAX_TENTATIVAS(self) -> int:
        return cfg.CAPTCHA_MAX_TENTATIVAS_DOC

    def _auto_api_download(self, numero_id: str, cod_pedido: str | None) -> tuple[bytes | None, str | None]:
        maximo = self._CAPTCHA_MAX_TENTATIVAS
        for tentativa in range(1, maximo + 1):
            resultado = self._auto_api_download_once(numero_id, cod_pedido, tentativa)
            if resultado is not None:
                return resultado
            if tentativa < maximo:
                log.info("CAPTCHA rejeitado — nova tentativa (%d/%d) com token novo",
                         tentativa + 1, maximo)
                # NÃO recarregar a página aqui. O token vem de um solver
                # proxyless (não do widget), então nada precisa ser renovado no
                # DOM — e um page.goto() quebra o contexto do fetch same-origin
                # da validação, que passa a falhar com "TypeError: Failed to
                # fetch" (medido: 2/2 retries perdidos assim). Basta a pausa.
                time.sleep(2.0)
        return None, None

    def _auto_api_download_once(self, numero_id: str, cod_pedido: str | None,
                                tentativa: int = 1) -> tuple[bytes | None, str | None] | None:
        """Uma tentativa completa (solve → valida → baixa).

        Retorna a tupla de resultado quando há um desfecho definitivo, ou None
        quando vale a pena tentar de novo com um token novo.
        """
        site_key = _extract_recaptcha_sitekey(self._page.content()) or _RECAPTCHA_SITEKEY

        # Open the CAPTCHA modal so the reCAPTCHA widget is rendered in our session
        try:
            self._page.click(f"img.salvaDocumento[id='{numero_id}']", force=True, timeout=5000)
            time.sleep(1.5)
            log.debug("Modal CAPTCHA aberto (NumeroID=%s…)", numero_id[:20])
        except Exception as e:
            log.warning("Falha ao abrir modal: %s — continuando mesmo assim", e)

        from captcha_solver import solve_recaptcha_v2
        log.info("Solicitando solução de reCAPTCHA via %s… (tentativa %d/%d)",
                 self._captcha_mode, tentativa, self._CAPTCHA_MAX_TENTATIVAS)
        token = solve_recaptcha_v2(site_key, self._page.url, mode=self._captcha_mode)
        if not token:
            log.error("Solver automático falhou para NumeroID=%s", numero_id[:20])
            return None, None
        log.info("Token recebido (%d caracteres)", len(token))

        # Inject token into the page's grecaptcha widget (so any JS reads it)
        try:
            self._page.evaluate(
                """(t) => {
                    const ta = document.querySelector('textarea[name=\"g-recaptcha-response\"]');
                    if (ta) { ta.value = t; ta.innerHTML = t; }
                }""",
                token,
            )
        except Exception as e:
            log.debug("Inject token no widget falhou: %s", e)

        # Step 1: validate via page.evaluate fetch (request goes from page's JS context)
        val_script = (
            "async ({numId, captcha}) => {"
            "  try {"
            "    const url = '/pePI/servlet/ImagemDocumentoPdfController'"
            "             + '?action=validaCaptcha&NumID=' + encodeURIComponent(numId)"
            "             + '&captcha=' + encodeURIComponent(captcha);"
            "    const r = await fetch(url, {credentials: 'include'});"
            "    const text = await r.text();"
            "    return {status: r.status, body: text};"
            "  } catch (e) { return {error: e.toString()}; }"
            "}"
        )

        val_result = None
        for attempt in range(2):
            try:
                val_result = self._page.evaluate(val_script, {"numId": numero_id, "captcha": token})
            except Exception as e:
                log.error("Falha na validação de CAPTCHA (evaluate): %s", e)
                return None, None

            if not val_result.get("error"):
                break

            err = val_result["error"]
            log.warning("Tentativa %d: erro no fetch — %s", attempt + 1, err)
            if attempt == 0:
                # Likely session expired during 2captcha solve. Refresh page and retry.
                try:
                    self._page.goto(self._page.url, wait_until="domcontentloaded", timeout=60_000)
                    time.sleep(1.0)
                except Exception:
                    pass

        if val_result is None or val_result.get("error"):
            log.error("Erro no fetch de validação após retry: %s", val_result.get("error") if val_result else "no result")
            return None, None

        body_text = val_result.get("body", "")
        try:
            import json as _json
            val_json = _json.loads(body_text)
        except Exception:
            log.error("validaCaptcha resposta inesperada (status=%s): %s",
                      val_result.get("status"), body_text[:200])
            return None, None
        if val_json.get("category") != "success":
            log.error("CAPTCHA rejeitado pelo INPI: %s", val_json.get("message", str(val_json)))
            # None (e não (None, None)) sinaliza "vale tentar com token novo" ao
            # laço de _auto_api_download. O token já foi pago ao solver, mas o
            # INPI o recusou — reenviá-lo seria recusado de novo.
            return None

        self._captcha_count += 1
        log.info("CAPTCHA validado pelo INPI: %s", val_json.get("message", ""))

        # Step 2: download PDF (page.request.get is fine here — no anti-bot check on this URL)
        params: dict[str, str] = {
            "CodDiretoria": "200",
            "NumeroID": numero_id,
            "certificado": "",
            "numeroProcesso": "",
            "ipasDoc": "",
        }
        if cod_pedido:
            params["codPedido"] = cod_pedido

        try:
            pdf_resp = self._page.request.get(_DOWNLOAD_URL, params=params, timeout=120_000)
            body = pdf_resp.body()
            if len(body) > 1000 and body[:4] == b"%PDF":
                log.info("PDF OK: %d bytes", len(body))
                return body, pdf_resp.url
            log.warning("Resposta não é PDF (status=%d primeiros bytes=%s)", pdf_resp.status, body[:20])
            # Debug: salva o HTML em /tmp pra inspeção quando não for PDF
            try:
                from pathlib import Path as _P
                dbg = _P('/tmp/inpi_non_pdf_response.html')
                dbg.write_bytes(body)
                log.warning("Debug: HTML salvo em %s (%d bytes)", dbg, len(body))
            except Exception:
                pass
        except Exception as e:
            log.error("Falha no download do PDF: %s", e)

        return None, None


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _extract_metadata(html: str) -> dict:
    """Extract structured fields from the pePI process detail page."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    result: dict = {
        "procurador": _extract_labeled_list(text, ["(74)", "Procurador"]),
        "inventores": _extract_labeled_list(text, ["(72)", "Inventor"]),
        "prioridade": _extract_prioridade(text),
        "data_concessao": _extract_date_field(text, ["(47)", "Data da Concessão:", "Data de Concessão:"]),
        "classificacao_ipc": _extract_ipc(text),
    }

    # Clean empties
    for k in list(result):
        if result[k] in (None, [], ""):
            result[k] = None

    log.debug("Metadata: procurador=%s inventores=%s", bool(result.get("procurador")), bool(result.get("inventores")))
    return result


def _extract_ipc(text: str) -> list[str] | None:
    """Extrai códigos IPC da página de detalhe do pePI (INID 51).

    Formato típico: "(51) Classificação IPC: A61N 5/067 A61N 5/067 (2000.01 )
    Terapia por radiação; ... (54) Título:". Múltiplos códigos vêm separados por
    ';' (ex.: "H04M 11/00 ; E21B 47/18"). Retorna a lista deduplicada preservando
    ordem — o primeiro é o IPC primário (sua letra inicial = seção técnica).
    """
    idx = text.find("Classificação IPC:")
    if idx < 0:
        return None
    frag = text[idx + len("Classificação IPC:"):]
    # Para no próximo INID (o campo seguinte é sempre (54) Título)
    frag = re.split(r"\(54\)|Título", frag)[0]
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(r"[A-H]\d{2}[A-Z]\s*\d{1,4}/\d{1,6}", frag):
        code = re.sub(r"\s+", " ", m.group(0)).strip()
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out or None


def _extract_labeled_list(text: str, labels: list[str]) -> list[str] | None:
    for label in labels:
        idx = text.find(label)
        if idx < 0:
            continue
        fragment = text[idx + len(label): idx + len(label) + 500].strip()
        # Stop at next INID code or section header
        fragment = re.split(r"\(\d+\)|\bANUIDEID\b|\bVer todas\b|\bAnuidades\b", fragment)[0]
        names = [n.strip() for n in re.split(r"[;/\n|]", fragment) if n.strip()]
        filtered = [n for n in names if 2 < len(n) < 150]
        if filtered:
            return filtered[:20]
    return None


def _extract_prioridade(text: str) -> list[dict] | None:
    for label in ["(30)", "Prioridade"]:
        idx = text.find(label)
        if idx < 0:
            continue
        fragment = text[idx: idx + 500]
        entries = []
        for m in re.finditer(r"(\w{2,3})\s+(\d[\d./\-]{5,15})\s+(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})", fragment):
            entries.append({"pais": m.group(1), "numero": m.group(2), "data": m.group(3)})
        if entries:
            return entries
    return None


def _extract_date_field(text: str, labels: list[str]) -> str | None:
    for label in labels:
        idx = text.find(label)
        if idx < 0:
            continue
        fragment = text[idx + len(label): idx + len(label) + 30].strip()
        m = re.search(r"\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2}", fragment)
        if m and m.group(0) not in ("--", "-"):
            return m.group(0)
    return None


def _find_salva_documento_img(html: str, rpi_numero: int, codigo: str, cod_pedido: str | None) -> dict | None:
    """Find the salvaDocumento img element that corresponds to the given RPI + despacho code.

    The detail page table has alternating row types:
      Row A: despacho code link (with hidden tooltip div) + PDF icon (salvaDocumento img) + ...
      Row B (#E0E0E0): RPI number + date + ...

    Strategy:
      1. Find Row B where first cell = str(rpi_numero)
      2. Get the previous sibling TR (Row A)
      3. Find .salvaDocumento img in Row A

    Fallback: find any salvaDocumento img whose parent context contains both rpi_numero and codigo.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: find E0E0E0 rows with matching RPI number
    rpi_str = str(rpi_numero)
    for tr in soup.find_all("tr", attrs={"bgcolor": "#E0E0E0"}):
        cells = tr.find_all("td")
        if not cells:
            continue
        first_cell_text = cells[0].get_text(strip=True)
        if first_cell_text != rpi_str:
            continue
        # Found the RPI row — get previous sibling TR
        prev_tr = tr.find_previous_sibling("tr")
        if prev_tr:
            img = prev_tr.find("img", class_="salvaDocumento")
            if img:
                log.debug("PDF img encontrada via rpi_row match: NumeroID=%s", img.get("id", "")[:30])
                return _img_attrs(img, cod_pedido)

    # Strategy 2: find img where context TR contains both rpi_numero and codigo text
    for img in soup.find_all("img", class_="salvaDocumento"):
        tr = img.find_parent("tr")
        if not tr:
            continue
        # Check this TR and all sibling TRs within 3 rows
        context = tr.get_text(" ", strip=True)
        for sibling in list(tr.next_siblings)[:3]:
            if hasattr(sibling, "get_text"):
                context += " " + sibling.get_text(" ", strip=True)
        if rpi_str in context and codigo in context:
            log.debug("PDF img encontrada via context match: NumeroID=%s", img.get("id", "")[:30])
            return _img_attrs(img, cod_pedido)

    # Strategy 3: if only one img exists (process has only one such despacho), use it
    imgs = soup.find_all("img", class_="salvaDocumento")
    if len(imgs) == 1:
        log.debug("PDF img única encontrada (fallback): NumeroID=%s", imgs[0].get("id", "")[:30])
        return _img_attrs(imgs[0], cod_pedido)

    # Strategy 4: find img whose surrounding despacho div contains the codigo
    for img in imgs:
        tr = img.find_parent("tr")
        if not tr:
            continue
        tr_text = tr.get_text(" ", strip=True)
        if codigo in tr_text:
            log.debug("PDF img encontrada via codigo in TR: %s", img.get("id", "")[:30])
            return _img_attrs(img, cod_pedido)

    log.warning("Nenhum img.salvaDocumento encontrado para rpi=%d cod=%s (%d imgs no total)",
                rpi_numero, codigo, len(imgs))
    return None


def _extract_publicacoes_table(html: str, cod_pedido: str | None) -> list[dict]:
    """Extract ALL rows from the 'Publicações' table on the pePI detail page.

    Layout real INPI: cada publicação está numa única <tr> (zebra-striped via
    bgcolor=white/#E0E0E0). Colunas: RPI | data | codigo | descrição | img.salvaDocumento.
    Múltiplos imgs.salvaDocumento podem aparecer na mesma tr (ex: doc principal +
    anexo) — pegamos o primeiro.

    Returns list of dicts (ordem da página: mais recente → mais antiga):
        [{rpi: 2887, rpi_data: '21/04/2026', codigo: '9.1',
          img_id: 'NNN', cod_pedido: 'XXX'}, ...]

    Linhas sem img.salvaDocumento são ignoradas. `rpi_data` raw 'dd/mm/yyyy'.
    Desduplica por (rpi, codigo) — primeira ocorrência vence.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[tuple[int, str]] = set()

    # Estratégia: cada tr com img.salvaDocumento é uma publicação. Colunas TD da
    # esquerda pra direita. Aceitar bgcolor=white ou #E0E0E0 (zebra) ou nenhum.
    for img in soup.find_all("img", class_="salvaDocumento"):
        tr = img.find_parent("tr")
        if not tr:
            continue
        cells = tr.find_all("td", recursive=False) or tr.find_all("td")
        if len(cells) < 3:
            continue

        rpi_text = cells[0].get_text(strip=True)
        if not rpi_text.isdigit():
            continue
        rpi_num = int(rpi_text)

        rpi_data = cells[1].get_text(strip=True)

        # 3ª célula: código (ex "9.1", "6.1", "9.1.4"). Pode ter texto extra.
        codigo_text = cells[2].get_text(" ", strip=True)
        m = re.match(r"\s*(\d{1,2}\.\d{1,2}(?:\.\d+)?)", codigo_text)
        if not m:
            continue
        codigo = m.group(1)

        key = (rpi_num, codigo)
        if key in seen:
            continue
        seen.add(key)

        attrs = _img_attrs(img, cod_pedido)
        out.append({
            "rpi": rpi_num,
            "rpi_data": rpi_data,
            "codigo": codigo,
            "img_id": attrs["id"],
            "cod_pedido": attrs.get("codPedido") or cod_pedido,
        })

    log.debug("Publicações extraídas: %d linhas", len(out))
    return out


# Tabela "Serviços" (petições do depositante) — layout distinto da Publicações:
#   | Serviço | Pgo | Protocolo     | Data       | Imagens | Cliente | Delivery | Data |
#   | 207     | ✓   | 870250037303  | 08/05/2025 | [PDF]   | …       | -        | -    |
# Heurística para diferenciar de Publicações: cell[0] = código de 3 dígitos sem
# ponto + cell[2] = protocolo numérico longo (≥10 dígitos) + cell[3] = data
# dd/mm/yyyy. Aceite da "Declaração de Finalidade" precisa estar feito antes.
_RE_PROTOCOLO_SERVICO = re.compile(r"^\d{10,}$")
_RE_DATA_BR = re.compile(r"^(\d{2}/\d{2}/\d{4})$")
# Código da tabela Serviços: linha cell[0] pode vir com texto colado
# (ex: "248Descrição do Serviço248Alte..."). Capturamos os 3 primeiros dígitos
# que precedem qualquer não-dígito. Range válido: 100-299 (petições do depositante).
_RE_CODIGO_SERVICO_PREFIX = re.compile(r"^(\d{3})\D")


def _codigo_servico_from_text(text: str) -> str | None:
    """Extrai código de 3 dígitos do início do texto (ignora descrição colada)."""
    m = _RE_CODIGO_SERVICO_PREFIX.match(text or "")
    if not m:
        # Fallback: texto que SEJA exatamente 3 dígitos
        if (text or "").strip().isdigit() and len(text.strip()) == 3:
            return text.strip()
        return None
    cod = m.group(1)
    # Limita range: petições do depositante são 1xx-2xx; despachos do INPI usam X.Y
    if cod.startswith(("1", "2")):
        return cod
    return None


def _servicos_visiveis(html: str) -> bool:
    """Verifica se a tabela 'Serviços' (com petições) está renderizada na página."""
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img", class_="salvaDocumento"):
        tr = img.find_parent("tr")
        if not tr:
            continue
        cells = tr.find_all("td", recursive=False) or tr.find_all("td")
        if len(cells) < 4:
            continue
        if _codigo_servico_from_text(cells[0].get_text(strip=True)):
            return True
    return False


def _find_first_match(cells, regex):
    """Retorna o texto do primeiro <td> cujo conteúdo bate em regex (group 1)."""
    for c in cells:
        t = c.get_text(strip=True)
        m = regex.match(t) if regex.match(t) else None
        if m:
            return m.group(1) if m.groups() else t
    return ""


def _extract_servicos_table(html: str, cod_pedido: str | None) -> list[dict]:
    """Extrai linhas da tabela 'Serviços' (petições do depositante).

    O layout real do pePI tem cells em ordem variável e textos colados (código +
    descrição sem separador). Estratégia robusta:
      - cell[0]: extrai código de 3 dígitos no início (regex `^\\d{3}\\D`)
      - protocolo: primeiro cell com `^\\d{10,}$` (números de protocolo)
      - data: primeiro cell com formato dd/mm/yyyy

    Retorna dicts com: codigo, protocolo, data (ISO yyyy-mm-dd), img_id, cod_pedido.
    Dedup por (codigo, protocolo).
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for img in soup.find_all("img", class_="salvaDocumento"):
        tr = img.find_parent("tr")
        if not tr:
            continue
        cells = tr.find_all("td", recursive=False) or tr.find_all("td")
        if len(cells) < 4:
            continue

        codigo = _codigo_servico_from_text(cells[0].get_text(strip=True))
        if not codigo:
            continue  # não é linha de Serviços (provavelmente Publicações)

        protocolo = _find_first_match(cells, _RE_PROTOCOLO_SERVICO)
        if not protocolo:
            continue  # sem protocolo identificável; pula com segurança

        data_raw = _find_first_match(cells, _RE_DATA_BR)
        data_iso = _data_br_to_iso(data_raw) if data_raw else ""

        key = (codigo, protocolo)
        if key in seen:
            continue
        seen.add(key)

        attrs = _img_attrs(img, cod_pedido)
        out.append({
            "codigo": codigo,
            "protocolo": protocolo,
            "data": data_iso,
            "data_raw": data_raw,
            "img_id": attrs["id"],
            "cod_pedido": attrs.get("codPedido") or cod_pedido,
        })

    log.debug("Serviços extraídos: %d linhas", len(out))
    return out


def _data_br_to_iso(raw: str) -> str:
    """Converte 'dd/mm/yyyy' → 'yyyy-mm-dd'. Retorna '' em formatos inesperados."""
    parts = raw.split("/")
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
    return ""


def _img_attrs(img, fallback_cod_pedido: str | None) -> dict:
    return {
        "id": img.get("id", ""),
        "codPedido": img.get("codPedido") or img.get("codpedido") or fallback_cod_pedido,
        "value": img.get("value", ""),
        "name": img.get("name", ""),
        "ipasDoc": img.get("ipasDoc") or img.get("ipasdoc", ""),
    }


def _extract_recaptcha_sitekey(html: str) -> str | None:
    for pattern in [
        r'data-sitekey=["\']([^"\']+)["\']',
        r'grecaptcha\.execute\(["\']([^"\']+)["\']',
        r'"sitekey":\s*"([^"]+)"',
    ]:
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return None
