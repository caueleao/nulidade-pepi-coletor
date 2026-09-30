"""Caminhos, constantes e utilidades compartilhadas do estudo."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

csv.field_size_limit(10_000_000)

RAIZ = Path(__file__).resolve().parent.parent
RAW = RAIZ / "data" / "raw"
DATA = RAIZ / "data"
DB = DATA / "nulidade.db"
PDFS = DATA / "pdfs"
ARTIGO = RAIZ / "artigo"

BASE_URL = "https://dadosabertos.inpi.gov.br/download/patentes"

# Arquivos do INPI Dados Abertos. O nome é o do arquivo remoto sem extensão.
ARQUIVOS = {
    "PATENTES_DESPACHOS": "despacho",
    "PATENTES_CLASSIFICACAO_IPC": "classificacao_ipc",
    "PATENTES_DADOS_BIBLIOGRAFICOS": "bibliografico",
    "PATENTES_CONTEUDO": "conteudo",
    "PATENTES_DEPOSITANTES": "depositante",
    "PATENTES_INVENTORES": "inventor",
    "PATENTES_PRIORIDADES": "prioridade",
    "PATENTES_PROCURADORES": "procurador",
    "PATENTES_VINCULOS": "vinculo",
}

# ---------------------------------------------------------------- despachos
# Famílias de código de despacho da tabela oficial do INPI (Patentes).
# Confirmadas em revistas.inpi.gov.br/rpi/download/despachos/200.
DESP = {
    "deferimento": {"9.1"},
    "indeferimento": {"9.2"},
    "indef_mantido_sem_recurso": {"9.2.4"},
    "recurso_interposto": {"12.2"},
    "recurso_negado": {"111"},
    "recurso_provido": {"100", "100.1"},
    "recurso_anulou_indef": {"100.2"},
    "recurso_nao_merito": {"130", "131", "134"},
    "concessao": {"16.1"},
    "concessao_anulada": {"16.4"},
    "pan_instaurado": {"17.1"},
    "pan_intimacao": {"205"},
    "pan_nulidade": {"200"},
    "pan_negado": {"201"},
    "pan_parcial": {"204"},
    "pan_nao_merito": {"210", "211", "212", "213"},
    "arquivamento": {
        "11.1", "11.1.1", "11.2", "11.4", "11.5", "11.6", "11.6.1", "11.11",
        "11.12", "11.17", "11.18", "11.20", "11.21", "11.34", "8.6",
    },
    "extincao": {"21.1", "21.2", "21.6", "21.7", "24.8", "24.10"},
    "restauracao": {"8.7", "24.4", "11.34.1"},
    "judicial_decisao": {"19.1", "15.14"},
    "sub_judice": {"15.23", "22.15"},
    "judicial_sobrestado": {"29.2", "29.3"},
    "exigencia": {"6.1", "6.7", "6.9", "6.20", "6.21", "6.22", "6.23"},
    "parecer": {"7.1"},
    "publicacao": {"3.1", "3.2"},
    "recurso_parecer": {"120"},
    "recurso_exigencia": {"121"},
}
CODIGO_FAMILIA = {c: f for f, cs in DESP.items() for c in cs}

# Petições das partes. Códigos de serviço (GRU) da tabela de retribuições do INPI.
PETICOES = {
    "210": "Subsídios ao exame técnico (art. 31)",
    "214": "Recurso (arts. 212 a 215)",
    "215": "Requerimento de nulidade administrativa (arts. 46 a 55)",
    "216": "Contestação do titular à nulidade (art. 52)",
    "272": "Manifestação sobre parecer em grau de recurso (art. 213)",
    "280": "Cumprimento de exigência em grau de recurso (art. 214)",
    "281": "Manifestação sobre parecer em 1ª instância (art. 36)",
    "282": "Manifestação em grau de nulidade (art. 53)",
}
PETICOES_RECURSO = ["214", "272", "280", "281", "210"]
PETICOES_PAN = ["215", "216", "282", "210"]

# ------------------------------------------------------------ classificação IA
# Nível 1: núcleo. Símbolos IPC que são, em si, técnicas de IA.
IA_NUCLEO = (
    r"^G06N"                      # sistemas computacionais baseados em modelos
    r"|^G06V"                     # reconhecimento visual (ex-G06K 9/ imagem)
    r"|^G06F18"                   # reconhecimento de padrões (ex-G06K 9/)
    r"|^G06F40"                   # tratamento de linguagem natural (ex-G06F 17/2x)
    r"|^G10L15"                   # reconhecimento de fala
    r"|^G10L17"                   # identificação de locutor
)
# Cuidado deliberado com o G10L: a OMPI lista 13, 15, 17 e 25. G10L 19 e 21
# são codificação e realce de sinal de voz, não técnica de IA. Incluí-los
# arrastava 1.779 pedidos de codec de áudio para dentro do grupo "IA núcleo",
# quase 40% do grupo, e era o que fazia toda a litigância "de IA" no Brasil
# parecer ser sobre IA quando é sobre codec.
# Nível 2: estratégia OMPI 2019, adotada pelo Radar Tecnológico nº 21 do INPI.
IA_PERIFERICA = (
    r"^G06T7"                     # análise de imagem
    r"|^G06T1/20"                 # arquitetura de processamento de imagem
    r"|^G06T2207"                 # indexação de análise de imagem
    r"|^G05B13/02"                # controle adaptativo
    r"|^G05D1"                    # controle de veículos sem piloto
    r"|^G06K9"                    # reconhecimento de padrões (símbolo pré-2023)
    r"|^G10L13|^G10L25"           # síntese de fala, análise de fala
    r"|^G06F17/2[78]"             # PLN (símbolo pré-2023)
    r"|^G16H50"                   # TI médica para predição e diagnóstico
)
RE_IA_NUCLEO = re.compile(IA_NUCLEO)
RE_IA_PERIFERICA = re.compile(IA_PERIFERICA)


def normaliza_ipc(simbolo: str) -> str:
    """'G06N  3/08' -> 'G06N3/08'."""
    return re.sub(r"\s+", "", (simbolo or "").upper())


def nivel_ia(simbolo: str) -> int:
    """0 = não-IA, 1 = núcleo, 2 = periférica."""
    s = normaliza_ipc(simbolo)
    if RE_IA_NUCLEO.match(s):
        return 1
    if RE_IA_PERIFERICA.match(s):
        return 2
    return 0


def grupo_tecnico(nivel: int, simbolos: list[str]) -> str:
    """Grupos mutuamente exclusivos usados em todo o estudo."""
    if nivel == 1:
        return "IA nucleo"
    if nivel == 2:
        return "IA periferica"
    s = [normaliza_ipc(x) for x in simbolos]
    if any(x.startswith("G06Q") for x in s):
        return "G06Q negocios"
    if any(x.startswith("G06") for x in s):
        return "G06 software"
    if s:
        return "demais areas"
    return "sem IPC"


GRUPOS = ["IA nucleo", "IA periferica", "G06Q negocios", "G06 software",
          "demais areas", "sem IPC"]


# --------------------------------------------------------------------- util
def conecta(somente_leitura: bool = False) -> sqlite3.Connection:
    if somente_leitura:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(DB)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    con.row_factory = sqlite3.Row
    return con


def sha256(caminho: Path, blocos: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as f:
        while bloco := f.read(blocos):
            h.update(bloco)
    return h.hexdigest()


def detecta_delimitador(caminho: Path) -> str:
    """O dicionário oficial diz ';', os arquivos vêm com ','. Nunca confiar na doc.

    Só o delimitador é inferido. O resto da semântica é RFC 4180 (`csv.excel`):
    o Sniffer infere `doublequote=False` neste arquivo e, com isso, funde 16
    registros de `PATENTES_DESPACHOS.csv` silenciosamente.
    """
    with caminho.open("r", encoding="utf-8", errors="replace") as f:
        amostra = f.read(64 * 1024)
    try:
        return csv.Sniffer().sniff(amostra, delimiters=",;|\t").delimiter
    except csv.Error:
        return ","


def dialeto(delim: str) -> type[csv.Dialect]:
    """csv.excel com o delimitador detectado."""
    class _D(csv.excel):
        delimiter = delim
    return _D


def log(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


def salva_json(caminho: Path, obj) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
