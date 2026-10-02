#!/usr/bin/env python3
"""Baixa o texto das decisões das ações de nulidade no eproc da JFRJ (TRF2).

O DataJud dá o desfecho codificado, mas não o porquê. A consulta pública do
eproc mostra, sem login, os documentos públicos de cada processo — sentença,
decisões, despachos — com o texto integral. O caminho, medido em 2026-09-30:

  1. a busca por número exige um Cloudflare Turnstile (resolvido pela 2captcha,
     ~R$ 0,01) e, depois de resolvido, a função `callbackCloudflare` da página;
  2. o documento abre numa moldura que carrega o texto por AJAX
     (`acessar_documento_implementacao`); por isso a leitura espera `#divdochtml`;
  3. o portal devolve 429 ("Too Many Requests") depois de ~6 processos em
     ~15 minutos: daí o intervalo longo entre processos e a pausa ao ver 429.

Só processos do eproc (número começando em 5) da Seção Judiciária do RJ
(origem 51xx). Ordem: IA/software, depois patentes que também tiveram PAN no
INPI, depois o restante; dentro de cada grupo, sentença mais recente primeiro.

  python scripts/baixar_decisoes_trf2.py --limite 10
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

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.environ.get("BASEPATENTES_SCRIPTS",
                                  "/Users/caueleao/Documents/BasePatentes/scripts"))
try:
    import config  # noqa: F401  — carrega o .env da BasePatentes (CAPTCHA_API_KEY)
except Exception:
    pass
import comum as c

log = logging.getLogger("trf2")

# 1º grau no eproc da JFRJ; 2º grau (acórdãos) no eproc do próprio TRF2. Mesmo
# sistema, mesmo Turnstile, servidores e limites de acesso separados.
INSTANCIAS = {
    "g1": {"base": "https://eproc-consulta.jfrj.jus.br/eproc/", "fila": "fila_trf2"},
    "g2": {"base": "https://eproc-consulta.trf2.jus.br/eproc/", "fila": "fila_trf2_g2"},
}
CONSULTA = "externo_controlador.php?acao=processo_consulta_publica"
INSTANCIA = "g1"
BASE = INSTANCIAS["g1"]["base"]
BUSCA = BASE + CONSULTA
# Decisões que dizem o porquê. Despachos de expediente (ATOORD, DESPADEC de
# mero andamento) ficam de fora por padrão; --todos os inclui.
ROTULOS_DECISAO = re.compile(r"^(SENT|ACOR|VOTO|RELVOTO|EMENTA|DEC|DESPADEC)")
ROTULOS_MERITO = re.compile(r"^(SENT|ACOR|VOTO|RELVOTO|EMENTA)")
GRUPOS_IA = ("IA nucleo", "IA periferica", "G06Q negocios", "G06 software")

SCHEMA = """
CREATE TABLE IF NOT EXISTS fila_trf2 (
    num_cnj       TEXT PRIMARY KEY,
    prioridade    INTEGER,
    status        TEXT NOT NULL DEFAULT 'fila',   -- fila|visitado|nao_encontrado|erro
    n_docs        INTEGER,
    erro          TEXT,
    atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS fila_trf2_g2 (
    num_cnj       TEXT PRIMARY KEY,
    prioridade    INTEGER,
    status        TEXT NOT NULL DEFAULT 'fila',
    n_docs        INTEGER,
    erro          TEXT,
    atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS decisao_judicial (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    num_cnj       TEXT NOT NULL,
    rotulo        TEXT NOT NULL,     -- SENT1, ACOR2, DESPADEC1...
    evento        TEXT,              -- linha do evento como aparece no eproc
    doc_id        TEXT,
    texto         TEXT,
    chars         INTEGER,
    sha256        TEXT,
    baixado_em    TEXT,
    instancia     TEXT DEFAULT 'g1',  -- g1 (JFRJ) | g2 (TRF2)
    UNIQUE (num_cnj, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_decjud_cnj ON decisao_judicial(num_cnj);
"""


def _agora() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def monta_fila(con) -> None:
    """Enfileira as ações de nulidade do eproc/JFRJ, sem mexer no que já tem status."""
    con.execute(f"""
        INSERT OR IGNORE INTO fila_trf2 (num_cnj, prioridade)
        SELECT j.num_cnj,
               MIN(CASE WHEN p.grupo IN ({','.join('?' * len(GRUPOS_IA))}) THEN 1
                        WHEN j.numero_inpi IN (SELECT numero_inpi FROM caso WHERE via='pan') THEN 2
                        ELSE 3 END)
        FROM judicial j LEFT JOIN pedido p ON p.numero_inpi = j.numero_inpi
        WHERE j.natureza IN ('nulidade', 'nulidade_inferida')
          AND j.num_cnj LIKE '5%' AND substr(j.num_cnj, 14, 3) = '402'
          AND substr(j.num_cnj, 17, 2) = '51'
        GROUP BY j.num_cnj""", GRUPOS_IA)
    con.commit()


def monta_fila_g2(con) -> None:
    """2º grau: processos já visitados no 1º grau que o DataJud mostra no G2."""
    con.execute("""
        INSERT OR IGNORE INTO fila_trf2_g2 (num_cnj, prioridade)
        SELECT f.num_cnj, f.prioridade FROM fila_trf2 f
        WHERE f.status = 'visitado'
          AND EXISTS (SELECT 1 FROM datajud d WHERE d.num_cnj = f.num_cnj AND d.grau = 'G2')""")
    con.commit()


def turnstile(sitekey: str, url: str) -> str:
    key = os.environ.get("CAPTCHA_API_KEY_2CAPTCHA") or os.environ["CAPTCHA_API_KEY"]
    r = requests.post("https://2captcha.com/in.php", data={
        "key": key, "method": "turnstile", "sitekey": sitekey, "pageurl": url, "json": 1},
        timeout=30).json()
    if r.get("status") != 1:
        raise RuntimeError(f"2captcha recusou: {r.get('request')}")
    for _ in range(40):
        time.sleep(5)
        g = requests.get("https://2captcha.com/res.php", params={
            "key": key, "action": "get", "id": r["request"], "json": 1}, timeout=30).json()
        if g.get("status") == 1:
            return g["request"]
        if g.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha: {g.get('request')}")
    raise RuntimeError("2captcha: turnstile não resolvido em 200 s")


class Limite(Exception):
    """O portal respondeu 429."""


def _checa_429(pg) -> None:
    if "Too Many Requests" in pg.content()[:5000]:
        raise Limite()


def visita(pg, con, cnj: str, todos: bool) -> int:
    pg.goto(BUSCA, wait_until="domcontentloaded", timeout=90_000)
    _checa_429(pg)
    pg.locator("input[id^='txtNum']").first.fill(cnj, timeout=60_000)
    sk = re.search(r'data-sitekey="([^"]+)"', pg.content())
    if sk:
        tok = turnstile(sk.group(1), BUSCA)
        pg.evaluate("""t => {
            document.querySelectorAll("input[name='cf-turnstile-response']").forEach(e => e.value = t);
            if (typeof callbackCloudflare === 'function') callbackCloudflare(t); }""", tok)
        time.sleep(4)
    pg.locator("button:has-text('Consultar'), input[value='Consultar'], #sbmNovo").first.click()
    pg.wait_for_load_state("domcontentloaded", timeout=90_000)
    time.sleep(3)
    _checa_429(pg)
    if "Detalhes do Processo" not in pg.title():
        return -1                                        # não achou o processo
    lista = pg.locator("a:has-text('listar todos')")
    if lista.count():
        lista.first.click()
        pg.wait_for_load_state("domcontentloaded", timeout=90_000)
        time.sleep(3)

    alvo = ROTULOS_DECISAO if todos else ROTULOS_MERITO
    docs = []
    for a in pg.locator("a[href*='acessar_documento_publico']").all():
        rot = (a.inner_text() or "").strip()
        href = a.get_attribute("href") or ""
        m = re.search(r"doc=(\d+)", href)
        if not (rot and m and alvo.match(rot)):
            continue
        linha = a.locator("xpath=ancestor::tr[1]")
        evento = re.sub(r"\s+", " ", linha.inner_text())[:300] if linha.count() else None
        docs.append((rot, m.group(1), href, evento))
    ja = {r[0] for r in con.execute("SELECT doc_id FROM decisao_judicial WHERE num_cnj=?", (cnj,))}

    n = 0
    for rot, doc_id, href, evento in docs:
        if doc_id in ja:
            continue
        time.sleep(random.uniform(8, 15))
        pg.goto(BASE + href, wait_until="domcontentloaded", timeout=90_000)
        _checa_429(pg)
        try:
            pg.wait_for_function(
                "document.querySelector('#divdochtml') &&"
                " document.querySelector('#divdochtml').innerText.length > 200", timeout=60_000)
        except Exception:
            pass
        _checa_429(pg)
        texto = pg.inner_text("#divdochtml") if pg.locator("#divdochtml").count() else ""
        con.execute(
            "INSERT OR REPLACE INTO decisao_judicial (num_cnj, rotulo, evento, doc_id, texto,"
            " chars, sha256, baixado_em, instancia) VALUES (?,?,?,?,?,?,?,?,?)",
            (cnj, rot, evento, doc_id, texto, len(texto),
             hashlib.sha256(texto.encode()).hexdigest(), _agora(), INSTANCIA))
        con.commit()
        n += 1
        log.info("    %s %s: %d chars", cnj, rot, len(texto))
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limite", type=int, default=0)
    ap.add_argument("--instancia", choices=tuple(INSTANCIAS), default="g1")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--total-shards", type=int, default=1,
                    help="cada shard pega os processos de posição ≡ shard (mod N) na fila ordenada")
    ap.add_argument("--ate-minutos", type=float, default=0,
                    help="não começa processo novo depois de tantos minutos (0 = sem teto)")
    ap.add_argument("--sem-montar", action="store_true",
                    help="não reconstrói a fila (no CI a base enxuta não tem o censo)")
    ap.add_argument("--todos", action="store_true",
                    help="inclui decisões interlocutórias e despachos (DEC, DESPADEC)")
    ap.add_argument("--intervalo", type=float, default=150,
                    help="segundos médios entre processos (o portal dá 429 se for rápido)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    global INSTANCIA, BASE, BUSCA
    INSTANCIA = args.instancia
    BASE = INSTANCIAS[INSTANCIA]["base"]
    BUSCA = BASE + CONSULTA
    fila = INSTANCIAS[INSTANCIA]["fila"]

    con = c.conecta()
    con.execute("PRAGMA busy_timeout=30000")   # g1 e g2 escrevem na mesma base
    con.executescript(SCHEMA)
    if "instancia" not in {r[1] for r in con.execute("PRAGMA table_info(decisao_judicial)")}:
        con.execute("ALTER TABLE decisao_judicial ADD COLUMN instancia TEXT DEFAULT 'g1'")
    if not args.sem_montar:
        monta_fila(con)
        monta_fila_g2(con)
    casos = [r[0] for r in con.execute(
        f"SELECT f.num_cnj FROM {fila} f LEFT JOIN desfecho_judicial d USING(num_cnj)"
        " WHERE f.status IN ('fila','erro') ORDER BY f.prioridade, d.sentenca_data DESC,"
        " f.num_cnj")]
    # Fatiar depois de ordenar mantém a prioridade dentro de cada shard.
    casos = casos[args.shard::args.total_shards]
    if args.limite:
        casos = casos[:args.limite]
    log.info("%d processo(s) na fila do %s (%s)", len(casos), INSTANCIA, fila)
    inicio = time.monotonic()

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page()
        pg.on("dialog", lambda d: d.dismiss())
        i = 0
        while i < len(casos):
            cnj = casos[i]
            if args.ate_minutos and (time.monotonic() - inicio) / 60 > args.ate_minutos:
                log.info("teto de %.0f min atingido; parando antes de %s", args.ate_minutos, cnj)
                break
            log.info("[%d/%d] %s", i + 1, len(casos), cnj)
            try:
                n = visita(pg, con, cnj, args.todos)
            except Limite:
                log.warning("429 do portal; pausa de 15 min e repete %s", cnj)
                time.sleep(900)
                continue
            except Exception as e:
                log.warning("  erro em %s: %s", cnj, str(e)[:150])
                con.execute(f"UPDATE {fila} SET status='erro', erro=?, atualizado_em=?"
                            " WHERE num_cnj=?", (str(e)[:300], _agora(), cnj))
                con.commit()
                i += 1
                continue
            status = "nao_encontrado" if n < 0 else "visitado"
            con.execute(f"UPDATE {fila} SET status=?, n_docs=?, erro=NULL, atualizado_em=?"
                        " WHERE num_cnj=?", (status, max(n, 0), _agora(), cnj))
            con.commit()
            log.info("  -> %s, %d documento(s)", status, max(n, 0))
            i += 1
            time.sleep(random.uniform(0.7, 1.3) * args.intervalo)
        b.close()
    tot = con.execute("SELECT COUNT(*), COUNT(DISTINCT num_cnj) FROM decisao_judicial").fetchone()
    log.info("Fim. Base tem %d decisão(ões) de %d processo(s).", tot[0], tot[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
