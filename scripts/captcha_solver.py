"""API client for reCAPTCHA v2 solvers (2captcha / anti-captcha).

INPI usa reCAPTCHA v2 standard (não Enterprise).
Only used when config [captcha].mode != "manual".
"""
from __future__ import annotations
import logging
import time

import requests

import config as cfg

log = logging.getLogger(__name__)


def solve_recaptcha_v2(site_key: str, page_url: str, mode: str | None = None) -> str | None:
    """Solve a reCAPTCHA v2 token. Returns token string or None on failure."""
    mode = mode or cfg.CAPTCHA_MODE
    if mode == "2captcha":
        return _solve_2captcha(site_key, page_url)
    if mode == "anticaptcha":
        return _solve_anticaptcha(site_key, page_url)
    log.error("Modo de CAPTCHA desconhecido: %s", mode)
    return None


# Backwards-compat alias
solve_recaptcha_enterprise = solve_recaptcha_v2


# ---------------------------------------------------------------------------
# 2captcha
# ---------------------------------------------------------------------------

def _solve_2captcha(site_key: str, page_url: str) -> str | None:
    import os
    # Preferência: chave específica do solver; fallback: chave genérica
    api_key = os.environ.get("CAPTCHA_API_KEY_2CAPTCHA") or cfg.CAPTCHA_API_KEY
    if not api_key:
        log.error("CAPTCHA_API_KEY_2CAPTCHA (ou CAPTCHA_API_KEY) não configurada")
        return None

    try:
        resp = requests.post("https://2captcha.com/in.php", data={
            "key": api_key,
            "method": "userrecaptcha",
            "googlekey": site_key,
            "pageurl": page_url,
            "json": 1,
        }, timeout=30)
        data = resp.json()
    except Exception as e:
        log.error("2captcha submit falhou: %s", e)
        return None

    if data.get("status") != 1:
        log.error("2captcha submit rejeitado: %s", data.get("request"))
        return None

    task_id = data["request"]
    log.info("2captcha task %s enviada, aguardando resolução…", task_id)

    deadline = time.monotonic() + cfg.CAPTCHA_TIMEOUT
    time.sleep(15)
    while time.monotonic() < deadline:
        try:
            r = requests.get("https://2captcha.com/res.php", params={
                "key": api_key,
                "action": "get",
                "id": task_id,
                "json": 1,
            }, timeout=30)
            result = r.json()
        except Exception as e:
            log.warning("2captcha poll error: %s", e)
            time.sleep(5)
            continue

        if result.get("status") == 1:
            log.info("2captcha resolvido com sucesso")
            return result["request"]
        if result.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
            log.error("2captcha erro: %s", result.get("request"))
            return None
        time.sleep(5)

    log.error("2captcha timeout após %ds sem resposta", cfg.CAPTCHA_TIMEOUT)
    return None


# ---------------------------------------------------------------------------
# Anti-captcha (protocolo diferente)
# ---------------------------------------------------------------------------

def _solve_anticaptcha(site_key: str, page_url: str) -> str | None:
    import os
    api_key = os.environ.get("CAPTCHA_API_KEY_ANTICAPTCHA") or cfg.CAPTCHA_API_KEY
    if not api_key:
        log.error("CAPTCHA_API_KEY_ANTICAPTCHA (ou CAPTCHA_API_KEY) não configurada")
        return None

    try:
        resp = requests.post("https://api.anti-captcha.com/createTask", json={
            "clientKey": api_key,
            "task": {
                "type": "RecaptchaV2TaskProxyless",
                "websiteURL": page_url,
                "websiteKey": site_key,
            },
        }, timeout=30)
        data = resp.json()
    except Exception as e:
        log.error("anti-captcha createTask falhou: %s", e)
        return None

    if data.get("errorId"):
        log.error("anti-captcha erro: %s", data.get("errorDescription"))
        return None

    task_id = data["taskId"]
    log.info("anti-captcha task %s criada", task_id)

    deadline = time.monotonic() + cfg.CAPTCHA_TIMEOUT
    time.sleep(15)
    while time.monotonic() < deadline:
        try:
            # timeout=(connect, read): o read-timeout limita o intervalo ENTRE
            # bytes, não a chamada inteira — com um único valor, um servidor que
            # goteja resposta pendura o poll indefinidamente e o deadline acima
            # nunca é reavaliado (medido: um poll preso por 20min).
            r = requests.post("https://api.anti-captcha.com/getTaskResult", json={
                "clientKey": api_key,
                "taskId": task_id,
            }, timeout=(10, 20))
            result = r.json()
        except Exception as e:
            log.warning("anti-captcha poll error: %s", e)
            time.sleep(5)
            continue

        if result.get("status") == "ready":
            log.info("anti-captcha resolvido")
            return result["solution"]["gRecaptchaResponse"]
        if result.get("errorId"):
            log.error("anti-captcha erro: %s", result.get("errorDescription"))
            return None
        time.sleep(5)

    log.error("anti-captcha timeout após %ds", cfg.CAPTCHA_TIMEOUT)
    return None
