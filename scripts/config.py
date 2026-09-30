"""Load config.toml and resolve paths relative to project root."""
from __future__ import annotations
import os
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT / "config" / "config.toml"
_ENV_PATH = _ROOT / "config" / ".env"


def _load_env_file() -> None:
    """Load KEY=VALUE pairs from config/.env into os.environ (if file exists)."""
    if not _ENV_PATH.exists():
        return
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()


def _load() -> dict:
    with open(_CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


_cfg = _load()


def get(section: str, key: str | None = None):
    s = _cfg[section]
    return s if key is None else s[key]


def path(rel: str) -> Path:
    """Return absolute Path for a relative path from config."""
    return _ROOT / rel


ROOT = _ROOT
DB_PATH = path(_cfg["paths"]["db_path"])
RAW_DIR = path(_cfg["paths"]["raw_dir"])
LOG_DIR = path(_cfg["paths"]["log_dir"])
COMUNICADOS_DIR = path(_cfg["paths"]["comunicados_dir"])

CODIGOS_ALVO: list[str] = _cfg["despachos"]["codigos_alvo"]
CODIGOS_BIBLIOGRAFICOS: set[str] = set(_cfg["despachos"]["codigos_bibliograficos"])
DESPACHO_DIRS: dict[str, Path] = {k: path(v) for k, v in _cfg["despachos"]["dirs"].items()}
DESPACHO_LABELS: dict[str, str] = _cfg["despachos"]["labels"]

_cascata = _cfg.get("cascata", {})
CASCATA_GATILHO_CODIGOS: list[str] = _cascata.get("gatilho_codigos", ["9.1", "9.2"])
CASCATA_HISTORICO_CODIGOS: list[str] = _cascata.get(
    "historico_codigos", ["6.1", "6.21", "6.22", "6.23", "7.1"]
)
CASCATA_PETICOES_91_CODIGOS: list[str] = _cascata.get(
    "peticoes_91_codigos", ["207", "281"]
)

# Códigos de petição do depositante que carregam o bloco "Dados do Depositante
# (71)" na página 1 (nome, CPF/CNPJ, telefone, e-mail). O 200 (protocolo de
# depósito) é o preferencial da Base de Leads porque todo pedido tem um.
_leads = _cfg.get("leads", {})
LEADS_PETICAO_PREFERIDA: str = _leads.get("peticao_preferida", "200")
LEADS_DESPACHO_CODIGOS: list[str] = _leads.get(
    "despacho_codigos", ["6.1", "6.7", "6.9", "6.21", "6.22", "6.23", "7.1", "9.2"]
)


CAPTCHA_MAX_TENTATIVAS_DOC: int = _cfg["captcha"].get("max_tentativas_documento", 3)


def despacho_dir(codigo: str) -> Path:
    """Diretório de destino de um código de despacho ou serviço.

    `DESPACHO_DIRS[codigo]` direto levanta KeyError em códigos não mapeados no
    config (6.7, 6.9 e os serviços de fallback da Base de Leads), que aparecem
    de verdade na tabela Publicações/Serviços do pePI. Aqui eles ganham um
    diretório por convenção em vez de derrubar o download.
    """
    d = DESPACHO_DIRS.get(codigo)
    if d is not None:
        return d
    prefixo = "peticao" if codigo.isdigit() else "despacho"
    return path(f"data/{prefixo}_{codigo}")

RPI_INDEX_URL: str = _cfg["inpi"]["rpi_index_url"]
TXT_BASE_URL: str = _cfg["inpi"]["txt_base_url"]
BUSCA_BASE_URL: str = _cfg["inpi"]["busca_base_url"]
USER_AGENT: str = _cfg["inpi"]["user_agent"]

HTTP_TIMEOUT: int = _cfg["http"]["timeout_sec"]
MAX_RETRIES: int = _cfg["http"]["max_retries"]
RETRY_WAIT_MIN: int = _cfg["http"]["retry_wait_min"]
RETRY_WAIT_MAX: int = _cfg["http"]["retry_wait_max"]

PDF_ENABLED: bool = _cfg["pdf"]["enabled"]
PDF_MAX_POR_EXECUCAO: int = _cfg["pdf"]["max_por_execucao"]
PDF_BACKOFF_MAX: int = _cfg["pdf"]["backoff_max_tentativas"]

BROWSER_HEADLESS: bool = (
    True if os.environ.get("CI", "").lower() == "true"
    else _cfg["browser"]["headless"]
)
BROWSER_STORAGE_STATE: Path = path(_cfg["browser"]["storage_state_path"])
BROWSER_RATE_MIN: int = _cfg["browser"]["rate_min_sec"]
BROWSER_RATE_MAX: int = _cfg["browser"]["rate_max_sec"]
BROWSER_BATCH_SIZE: int = _cfg["browser"].get("batch_size", 10)
BROWSER_BATCH_PAUSE: int = _cfg["browser"].get("batch_pause_sec", 180)
BROWSER_NAV_TIMEOUT: int = _cfg["browser"]["nav_timeout_ms"]
BROWSER_SLOW_MO: int = _cfg["browser"]["slow_mo_ms"]

CAPTCHA_MODE: str = _cfg["captcha"]["mode"]
CAPTCHA_API_KEY_ENV: str = _cfg["captcha"]["api_key_env"]
CAPTCHA_API_KEY: str | None = os.environ.get(_cfg["captcha"]["api_key_env"])
CAPTCHA_TIMEOUT: int = _cfg["captcha"]["timeout_sec"]
CAPTCHA_MAX_PER_RUN: int = _cfg["captcha"]["max_per_run"]

PEPI_USER: str | None = os.environ.get("PEPI_USER") or None
PEPI_PASS: str | None = os.environ.get("PEPI_PASS") or None

_proxy_cfg = _cfg.get("proxy", {})
PROXY_ENABLED: bool = bool(_proxy_cfg.get("enabled", False))
PROXY_SERVER: str = _proxy_cfg.get("server", "")
PROXY_USER: str | None = os.environ.get(_proxy_cfg.get("user_env", "IPROYAL_USER"))
PROXY_PASS: str | None = os.environ.get(_proxy_cfg.get("pass_env", "IPROYAL_PASS"))
PROXY_COUNTRY: str = _proxy_cfg.get("country", "br")


def build_proxy(session_id: str | None = None) -> dict | None:
    """Build a Playwright proxy dict for IPRoyal Royal Residential.

    IPRoyal syntax: country/session flags are appended to PASSWORD (not username):
        password_country-br_session-XYZ

    If session_id is None: rotates IP per request (no sticky).
    """
    if not (PROXY_ENABLED and PROXY_SERVER and PROXY_USER and PROXY_PASS):
        return None
    pw_parts = [PROXY_PASS]
    if PROXY_COUNTRY:
        pw_parts.append(f"country-{PROXY_COUNTRY}")
    if session_id:
        pw_parts.append(f"session-{session_id}")
    return {
        "server": PROXY_SERVER,
        "username": PROXY_USER,
        "password": "_".join(pw_parts),
    }
