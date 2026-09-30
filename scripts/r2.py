#!/usr/bin/env python3
"""Ponte com o Cloudflare R2 para a coleta distribuída.

Layout no bucket, tudo sob o prefixo `nulidade/`:

  nulidade/fila.db.gz                     fila enxuta que cada shard recebe
  nulidade/pdfs/<via>/<processo>/<peça>   PDFs, na mesma árvore de `data/pdfs`
  nulidade/runs/<run>/shard_<k>.db.gz     base de cada shard ao fim (ou a cada sync)

Credenciais vêm do ambiente (R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID,
R2_SECRET_KEY): no CI, dos secrets; no Mac, do `config/.env` da BasePatentes,
carregado ao importar `config`.

Uso no CI:
  python scripts/r2.py baixar-fila
  python scripts/r2.py sync --run RUN --shard K     # PDFs novos + snapshot da base
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.environ.get("BASEPATENTES_SCRIPTS",
                                  "/Users/caueleao/Documents/BasePatentes/scripts"))
try:
    import config  # noqa: F401  — só para carregar o .env no Mac
except Exception:
    pass

import comum as c

PREFIXO = "nulidade"


def cliente():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_KEY"],
        config=Config(retries={"max_attempts": 5, "mode": "standard"}),
        region_name="auto",
    )


def bucket() -> str:
    return os.environ["R2_BUCKET"]


def lista(s3, prefixo: str) -> list[str]:
    chaves, token = [], None
    while True:
        kw = {"Bucket": bucket(), "Prefix": prefixo}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        chaves += [o["Key"] for o in r.get("Contents", [])]
        if not r.get("IsTruncated"):
            return chaves
        token = r["NextContinuationToken"]


def envia_gz(s3, origem: Path, chave: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as tmp:
        with open(origem, "rb") as f, gzip.open(tmp, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g)
    s3.upload_file(tmp.name, bucket(), chave)
    os.unlink(tmp.name)


def baixa_gz(s3, chave: str, destino: Path) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as tmp:
        s3.download_file(bucket(), chave, tmp.name)
    with gzip.open(tmp.name, "rb") as g, open(destino, "wb") as f:
        shutil.copyfileobj(g, f)
    os.unlink(tmp.name)


def snapshot(origem: Path, destino: Path) -> None:
    """Cópia consistente de uma base SQLite em uso (API de backup, não cp)."""
    src = sqlite3.connect(origem)
    dst = sqlite3.connect(destino)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def cmd_baixar_fila(_args) -> int:
    baixa_gz(cliente(), f"{PREFIXO}/fila.db.gz", c.DB)
    print(f"fila em {c.DB} ({c.DB.stat().st_size // 1024} KB)")
    return 0


def cmd_sync(args) -> int:
    s3 = cliente()
    ja = set(lista(s3, f"{PREFIXO}/pdfs/"))
    novos = 0
    for pdf in sorted(c.PDFS.rglob("*.pdf")):
        chave = f"{PREFIXO}/pdfs/{pdf.relative_to(c.PDFS).as_posix()}"
        if chave not in ja:
            s3.upload_file(str(pdf), bucket(), chave)
            novos += 1
    if c.DB.exists():
        with tempfile.TemporaryDirectory() as d:
            snap = Path(d) / "shard.db"
            snapshot(c.DB, snap)
            envia_gz(s3, snap, f"{PREFIXO}/runs/{args.run}/shard_{args.shard}.db.gz")
    print(f"sync: {novos} PDF(s) novo(s); base do shard {args.shard} enviada")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("baixar-fila").set_defaults(f=cmd_baixar_fila)
    s = sub.add_parser("sync")
    s.add_argument("--run", required=True)
    s.add_argument("--shard", required=True)
    s.set_defaults(f=cmd_sync)
    args = p.parse_args()
    return args.f(args)


if __name__ == "__main__":
    raise SystemExit(main())
