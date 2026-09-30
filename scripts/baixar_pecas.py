#!/usr/bin/env python3
"""Baixa as peças de cada caso julgado no pePI e guarda a base de auditoria.

Uma visita por processo, e dela sai tudo o que aquele caso tem: os despachos
da tabela "Publicações" e as petições das partes da tabela "Serviços". O CAPTCHA
é cobrado por documento, então a visita única não economiza CAPTCHA — economiza
a navegação, que é a parte lenta e a que expõe ao bloqueio.

Cada peça baixada vira uma linha em `peca`, com hash e contagem de páginas, e o
texto extraído vai para `peca_texto`. O resumo de cada peça é feito depois, por
`resumir_pecas.py`, para que uma falha de download não custe o trabalho de
análise nem o contrário.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(line_buffering=True)
sys.path.insert(0, str(Path(__file__).resolve().parent))
# No CI os módulos da BasePatentes vêm junto, no mesmo diretório deste script.
sys.path.insert(0, os.environ.get("BASEPATENTES_SCRIPTS",
                                  "/Users/caueleao/Documents/BasePatentes/scripts"))

import comum as c

import config as bp_cfg          # noqa: E402  (BasePatentes)
from inpi_browser import (       # noqa: E402
    BrowserSession,
    _extract_publicacoes_table,
    _extract_servicos_table,
)

log = logging.getLogger("pecas")

SCHEMA = """
CREATE TABLE IF NOT EXISTS peca (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    numero_inpi   TEXT NOT NULL,
    via           TEXT NOT NULL,
    tipo          TEXT NOT NULL,      -- despacho | peticao
    codigo        TEXT NOT NULL,
    rotulo        TEXT,
    rpi           INTEGER,
    data          TEXT,
    protocolo     TEXT,
    pdf_path      TEXT,
    sha256        TEXT,
    bytes         INTEGER,
    paginas       INTEGER,
    chars         INTEGER,
    baixado_em    TEXT,
    UNIQUE (numero_inpi, via, tipo, codigo, rpi, protocolo)
);
CREATE INDEX IF NOT EXISTS idx_peca_caso ON peca(numero_inpi, via);
CREATE INDEX IF NOT EXISTS idx_peca_codigo ON peca(codigo);

CREATE TABLE IF NOT EXISTS peca_texto (
    peca_id       INTEGER PRIMARY KEY,
    texto         TEXT
);

-- Sondagem: o que existe no pePI para cada caso, sem baixar nada. Navegar e
-- ler as tabelas não custa CAPTCHA; só o documento custa. Medir antes de
-- gastar diz quanto do corpus é de fato recuperável.
CREATE TABLE IF NOT EXISTS sonda (
    numero_inpi   TEXT NOT NULL,
    via           TEXT NOT NULL,
    achou_processo INTEGER,
    cod_pedido    TEXT,
    publicacoes   INTEGER,   -- linhas com documento na tabela Publicações
    alvos_com_doc INTEGER,   -- dessas, quantas são despacho que o caso quer
    alvos_na_fila INTEGER,
    servicos      INTEGER,   -- petições visíveis na tabela Serviços
    peticoes_alvo INTEGER,
    declaracao_ok INTEGER,
    codigos       TEXT,      -- JSON: códigos de despacho com documento
    servicos_cod  TEXT,      -- JSON: códigos de petição encontrados
    erro          TEXT,
    sondado_em    TEXT,
    PRIMARY KEY (numero_inpi, via)
);
"""


def _agora() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _slug(numero: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", numero).strip("_")


def _iso(raw: str) -> str | None:
    """'21/04/2026' -> '2026-04-21'."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", (raw or "").strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def extrai_texto(caminho: Path) -> tuple[int, str]:
    """Páginas e texto do PDF. Falha de leitura devolve texto vazio, nunca exceção."""
    try:
        import pdfplumber
        with pdfplumber.open(caminho) as pdf:
            partes = []
            for pag in pdf.pages:
                partes.append(pag.extract_text() or "")
            return len(pdf.pages), "\n".join(partes)
    except Exception as e:  # PDF corrompido, protegido ou imagem pura
        log.warning("texto não extraído de %s: %s", caminho.name, e)
        return 0, ""


def salva_peca(con, numero: str, via: str, tipo: str, codigo: str, rotulo: str,
               rpi: int | None, data: str | None, protocolo: str | None,
               pdf: bytes) -> int | None:
    destino = c.PDFS / via / _slug(numero)
    destino.mkdir(parents=True, exist_ok=True)
    nome = f"{codigo}_{rpi or protocolo or 'sn'}.pdf"
    caminho = destino / nome
    caminho.write_bytes(pdf)
    paginas, texto = extrai_texto(caminho)
    cur = con.execute(
        "INSERT OR REPLACE INTO peca (numero_inpi, via, tipo, codigo, rotulo,"
        " rpi, data, protocolo, pdf_path, sha256, bytes, paginas, chars,"
        " baixado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (numero, via, tipo, codigo, rotulo, rpi, data, protocolo,
         str(caminho.relative_to(c.RAIZ)), hashlib.sha256(pdf).hexdigest(),
         len(pdf), paginas, len(texto), _agora()))
    pid = cur.lastrowid
    con.execute("INSERT OR REPLACE INTO peca_texto VALUES (?,?)", (pid, texto))
    con.commit()
    return pid


PEPI = "https://busca.inpi.gov.br/pePI/"


def _rede_ok() -> bool:
    """O pePI responde daqui? Distingue queda de rede de caso realmente sem dados."""
    import urllib.request
    try:
        urllib.request.urlopen(PEPI, timeout=20).close()
        return True
    except Exception:
        return False


def _espera_rede() -> None:
    """Segura a fila até o pePI voltar. Sem isso, uma queda de rede de minutos
    marca a fila inteira como erro em segundos (aconteceu em 2026-09-30)."""
    t0 = time.monotonic()
    while not _rede_ok():
        if int(time.monotonic() - t0) % 600 < 60:
            log.warning("pePI inacessível há %.0f min; aguardando a rede voltar",
                        (time.monotonic() - t0) / 60)
        time.sleep(60)
    log.info("pePI de volta após %.0f min", (time.monotonic() - t0) / 60)


def _pausa(i: int) -> None:
    time.sleep(random.uniform(bp_cfg.BROWSER_RATE_MIN, bp_cfg.BROWSER_RATE_MAX))
    if i and i % bp_cfg.BROWSER_BATCH_SIZE == 0:
        log.info("  pausa de lote: %ds", bp_cfg.BROWSER_BATCH_PAUSE)
        time.sleep(bp_cfg.BROWSER_BATCH_PAUSE)


def sonda(browser, con, numero: str, via: str) -> dict:
    """Mede o que existe para este caso sem baixar documento algum."""
    import json as _json
    alvos = {r["codigo"] for r in con.execute(
        "SELECT codigo FROM fila_peca WHERE numero_inpi=? AND via=? AND tipo='despacho'",
        (numero, via))}
    peticoes_alvo = set(c.PETICOES_RECURSO if via == "recurso" else c.PETICOES_PAN)
    d = {"numero_inpi": numero, "via": via, "achou_processo": 0, "cod_pedido": None,
         "publicacoes": 0, "alvos_com_doc": 0, "alvos_na_fila": len(alvos),
         "servicos": 0, "peticoes_alvo": 0, "declaracao_ok": 0,
         "codigos": "[]", "servicos_cod": "[]", "erro": None}

    browser._ensure_logged_in()
    detail_url, cod_pedido = browser._search_process(numero)
    if not detail_url:
        d["erro"] = "processo não encontrado no pePI"
        return d
    d["achou_processo"] = 1
    d["cod_pedido"] = cod_pedido
    browser._page.goto(detail_url, wait_until="domcontentloaded",
                       timeout=max(bp_cfg.BROWSER_NAV_TIMEOUT, 120_000))
    try:
        browser._page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    pubs = _extract_publicacoes_table(browser._page.content(), cod_pedido)
    com_doc = [p for p in pubs if p.get("img_id")]
    d["publicacoes"] = len(com_doc)
    d["codigos"] = _json.dumps(sorted({p["codigo"] for p in com_doc}))
    d["alvos_com_doc"] = len({p["codigo"] for p in com_doc} & alvos)

    if cod_pedido:
        try:
            if browser._aceitar_declaracao(cod_pedido, detail_url):
                d["declaracao_ok"] = 1
                srv = [x for x in _extract_servicos_table(
                    browser._page.content(), cod_pedido) if x.get("img_id")]
                d["servicos"] = len(srv)
                d["servicos_cod"] = _json.dumps(sorted({x["codigo"] for x in srv}))
                d["peticoes_alvo"] = sum(1 for x in srv if x["codigo"] in peticoes_alvo)
        except Exception as e:
            d["erro"] = f"Serviços: {str(e)[:150]}"
    return d


def visita(browser, con, numero: str, via: str) -> dict:
    """Uma visita ao processo: baixa os despachos do caso e as petições das partes."""
    res = {"despachos": 0, "peticoes": 0, "erro": None}
    alvos = {r["codigo"]: r["rotulo"] for r in con.execute(
        "SELECT codigo, rotulo FROM fila_peca WHERE numero_inpi=? AND via=?"
        " AND tipo='despacho'", (numero, via))}
    ja = {(r[0], r[1]) for r in con.execute(
        "SELECT codigo, rpi FROM peca WHERE numero_inpi=? AND via=? AND tipo='despacho'",
        (numero, via))}
    peticoes_alvo = c.PETICOES_RECURSO if via == "recurso" else c.PETICOES_PAN

    browser._ensure_logged_in()
    detail_url, cod_pedido = browser._search_process(numero)
    if not detail_url:
        res["erro"] = "processo não encontrado no pePI"
        return res

    browser._page.goto(detail_url, wait_until="domcontentloaded",
                       timeout=max(bp_cfg.BROWSER_NAV_TIMEOUT, 120_000))
    try:
        browser._page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    html = browser._page.content()

    pubs = [p for p in _extract_publicacoes_table(html, cod_pedido)
            if p.get("codigo") in alvos and p.get("img_id")]
    log.info("  %s: %d despacho(s) alvo na tabela de Publicações", numero, len(pubs))
    for i, p in enumerate(pubs):
        if (p["codigo"], p["rpi"]) in ja:
            continue
        try:
            pdf = browser.fetch_pdf_by_img_id(p["img_id"], p.get("cod_pedido") or cod_pedido)
        except RuntimeError:
            raise                      # disjuntor de CAPTCHA: propaga
        except Exception as e:
            log.warning("    %s %s: %s", numero, p["codigo"], str(e)[:120])
            continue
        if pdf and pdf[:4] == b"%PDF" and len(pdf) > 1000:
            salva_peca(con, numero, via, "despacho", p["codigo"],
                       alvos.get(p["codigo"]), p["rpi"], _iso(p.get("rpi_data", "")),
                       None, pdf)
            res["despachos"] += 1
            log.info("    ok despacho %s rpi=%s (%d KB)", p["codigo"], p["rpi"],
                     len(pdf) // 1024)
        _pausa(i + 1)

    # Petições: exigem aceitar a Declaração de Finalidade. A ordem importa — o
    # modal do reCAPTCHA altera o DOM e o link some, por isso o aceite vem depois
    # dos despachos e re-navega sozinho.
    if cod_pedido:
        try:
            if browser._aceitar_declaracao(cod_pedido, detail_url):
                servicos = [s for s in _extract_servicos_table(
                    browser._page.content(), cod_pedido)
                    if s.get("codigo") in peticoes_alvo and s.get("img_id")]
                log.info("  %s: %d petição(ões) das partes", numero, len(servicos))
                jap = {r[0] for r in con.execute(
                    "SELECT protocolo FROM peca WHERE numero_inpi=? AND via=?"
                    " AND tipo='peticao'", (numero, via))}
                for i, s in enumerate(servicos):
                    if s.get("protocolo") in jap:
                        continue
                    try:
                        pdf = browser.fetch_pdf_by_img_id(
                            s["img_id"], s.get("cod_pedido") or cod_pedido)
                    except RuntimeError:
                        raise
                    except Exception as e:
                        log.warning("    petição %s: %s", s.get("codigo"), str(e)[:120])
                        continue
                    if pdf and pdf[:4] == b"%PDF" and len(pdf) > 1000:
                        salva_peca(con, numero, via, "peticao", s["codigo"],
                                   c.PETICOES.get(s["codigo"]), None,
                                   s.get("data"), s.get("protocolo"), pdf)
                        res["peticoes"] += 1
                        log.info("    ok petição %s prot=%s (%d KB)", s["codigo"],
                                 s.get("protocolo"), len(pdf) // 1024)
                    _pausa(i + 1)
        except RuntimeError:
            raise
        except Exception as e:
            log.warning("  %s: Serviços indisponíveis: %s", numero, str(e)[:150])
    return res


def sondagem(con, casos, args) -> int:
    import json as _json
    log.info("Sondagem de %d caso(s) — nenhum documento é baixado", len(casos))
    feitos = 0
    with BrowserSession(headless=not args.headed, captcha_mode=args.captcha) as b:
        for n, row in enumerate(casos, 1):
            numero, via = row["numero_inpi"], row["via"]
            try:
                d = sonda(b, con, numero, via)
            except Exception as e:
                log.warning("[%d/%d] %s: %s", n, len(casos), numero, str(e)[:150])
                continue
            con.execute(
                "INSERT OR REPLACE INTO sonda VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d["numero_inpi"], d["via"], d["achou_processo"], d["cod_pedido"],
                 d["publicacoes"], d["alvos_com_doc"], d["alvos_na_fila"],
                 d["servicos"], d["peticoes_alvo"], d["declaracao_ok"],
                 d["codigos"], d["servicos_cod"], d["erro"], _agora()))
            con.commit()
            feitos += 1
            log.info("[%d/%d] %s (%s): %d publicações com documento, %d/%d alvos,"
                     " %d serviços (%d alvo)%s", n, len(casos), numero, via,
                     d["publicacoes"], d["alvos_com_doc"], d["alvos_na_fila"],
                     d["servicos"], d["peticoes_alvo"],
                     f" | {d['erro']}" if d["erro"] else "")
            time.sleep(random.uniform(3, 7))   # navegação é barata; ainda assim, educação
    r = con.execute(
        "SELECT COUNT(*) n, SUM(achou_processo) achou, SUM(alvos_com_doc) alvos,"
        " SUM(alvos_na_fila) fila, SUM(peticoes_alvo) pet, AVG(publicacoes) pub"
        " FROM sonda").fetchone()
    print(f"\n  casos sondados            : {r['n']:,}")
    print(f"  encontrados no pePI       : {r['achou'] or 0:,}")
    print(f"  despachos-alvo com documento: {r['alvos'] or 0:,} de {r['fila'] or 0:,}"
          f" na fila ({100*(r['alvos'] or 0)/max(r['fila'] or 1,1):.0f}%)")
    print(f"  petições das partes       : {r['pet'] or 0:,}")
    print(f"  média de publicações com documento por caso: {r['pub'] or 0:.1f}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--estagio", default="A", help="A, B, C ou D")
    p.add_argument("--limite", type=int, default=0, help="0 = sem limite")
    p.add_argument("--via", choices=("pan", "recurso"), help="só esta via")
    p.add_argument("--shard", type=int, default=0, help="índice deste shard (0..N-1)")
    p.add_argument("--total-shards", type=int, default=1,
                   help="cada shard pega os casos de posição ≡ shard (mod N) na fila ordenada")
    p.add_argument("--ate-minutos", type=float, default=0,
                   help="não começa caso novo depois de tantos minutos (0 = sem teto)")
    p.add_argument("--captcha", default="anticaptcha")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--repetir-erros", action="store_true",
                   help="reprocessa casos marcados como erro")
    p.add_argument("--sondar", action="store_true",
                   help="só mede o que existe no pePI; não baixa nem gasta CAPTCHA")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    con = c.conecta()
    con.executescript(SCHEMA)

    estados = ("fila", "erro") if args.repetir_erros else ("fila",)
    # Ordem: prioridade de `priorizar_fila.py` e, dentro dela, a decisão mais recente.
    q = ("SELECT f.numero_inpi, f.via FROM fila_caso f LEFT JOIN caso ca"
         " ON ca.numero_inpi=f.numero_inpi AND ca.via=f.via"
         " WHERE f.estagio=? AND (? IS NULL OR f.via=?) AND f.status IN"
         f" ({','.join('?' * len(estados))})"
         " ORDER BY f.prioridade, ca.dt_decisao DESC, f.numero_inpi")
    casos = con.execute(q, (args.estagio, args.via, args.via, *estados)).fetchall()
    # Fatiar depois de ordenar mantém a prioridade dentro de cada shard.
    casos = casos[args.shard::args.total_shards]
    if args.limite:
        casos = casos[: args.limite]
    log.info("Estágio %s: %d caso(s) na fila", args.estagio, len(casos))
    if not casos:
        return 0

    if args.sondar:
        return sondagem(con, casos, args)

    ok = falhas = 0
    inicio = time.monotonic()
    with BrowserSession(headless=not args.headed, captcha_mode=args.captcha) as b:
        for n, row in enumerate(casos, 1):
            numero, via = row["numero_inpi"], row["via"]
            if args.ate_minutos and (time.monotonic() - inicio) / 60 > args.ate_minutos:
                log.info("teto de %.0f min atingido; parando antes de %s",
                         args.ate_minutos, numero)
                break
            log.info("[%d/%d] %s (%s)", n, len(casos), numero, via)
            try:
                r = visita(b, con, numero, via)
                if r["erro"] and not _rede_ok():
                    raise ConnectionError(r["erro"])   # "não encontrado" por falta de rede
            except RuntimeError as e:      # disjuntor
                log.error("disjuntor acionado: %s", e)
                con.execute("UPDATE fila_caso SET status='fila', erro=?,"
                            " atualizado_em=? WHERE numero_inpi=? AND via=?",
                            (str(e)[:300], _agora(), numero, via))
                con.commit()
                break
            except Exception as e:
                if not _rede_ok():
                    # Queda de rede não é defeito do caso: espera e segue; o caso
                    # continua 'fila' e volta na próxima execução.
                    log.warning("rede caiu em %s: %s", numero, str(e)[:120])
                    _espera_rede()
                    continue
                log.exception("erro em %s", numero)
                con.execute("UPDATE fila_caso SET status='erro', tentativas=tentativas+1,"
                            " erro=?, atualizado_em=? WHERE numero_inpi=? AND via=?",
                            (str(e)[:300], _agora(), numero, via))
                con.commit()
                falhas += 1
                continue
            total = r["despachos"] + r["peticoes"]
            status = "visitado" if not r["erro"] else "indisponivel"
            con.execute(
                "UPDATE fila_caso SET status=?, n_servicos=?, erro=?,"
                " tentativas=tentativas+1, atualizado_em=? WHERE numero_inpi=? AND via=?",
                (status, r["peticoes"], r["erro"], _agora(), numero, via))
            con.commit()
            ok += 1
            log.info("  -> %d peça(s): %d despacho(s), %d petição(ões)%s",
                     total, r["despachos"], r["peticoes"],
                     f" | {r['erro']}" if r["erro"] else "")

    n_pecas = con.execute("SELECT COUNT(*) FROM peca").fetchone()[0]
    log.info("Fim: %d caso(s) visitado(s), %d falha(s). Base tem %d peça(s).",
             ok, falhas, n_pecas)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
