"""
scripts/start_telegram.py
Utilities for the Telegram integration:
  - start_cloudflared(): launches a cloudflared tunnel and returns the public URL
  - set_webhook(): registers that URL with the Telegram Bot API

These are called from main.py during startup when TELEGRAM_ENABLED=true.
"""

import os
import re
import shutil
import subprocess
import threading
import time

import requests

_CLOUDFLARED_URL_RE = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")


def _drain(stream) -> None:
    for _ in stream:
        pass


def start_cloudflared(port: int) -> tuple[subprocess.Popen, str]:
    """
    Launch cloudflared and block until the public URL appears in its output.
    Returns (process, public_url).
    """
    cloudflared_path = shutil.which("cloudflared") or \
        r"C:\Users\Matias\AppData\Local\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe"
    if not os.path.exists(cloudflared_path):
        raise FileNotFoundError(
            "cloudflared no encontrado en PATH ni en la ruta por defecto. "
            "Descárgalo en: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
        )

    proc = subprocess.Popen(
        [cloudflared_path, "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    print("[cloudflared] Iniciando túnel...")
    public_url: str | None = None

    for line in proc.stdout:  # type: ignore[union-attr]
        print(f"[cloudflared] {line.rstrip()}")
        match = _CLOUDFLARED_URL_RE.search(line)
        if match:
            public_url = match.group(0)
            break

    if not public_url:
        proc.kill()
        raise RuntimeError("cloudflared no generó una URL pública.")

    threading.Thread(target=_drain, args=(proc.stdout,), daemon=True).start()
    return proc, public_url


def register_webhook(
    bot_token: str,
    public_url: str,
    path: str = "/webhook",
    secret: str = "",
    wait: int = 0,
) -> bool:
    """
    Registra el webhook en la API de Telegram. Devuelve si lo consiguió.

    `path` no es siempre "/webhook": en modo servidor la app de Telegram va
    montada en /tg dentro de la API principal, así que la ruta real es
    "/tg/webhook". Tenerlo a fuego era justo lo que hacía que el webhook no se
    registrara al desplegar.

    `secret` viaja de vuelta en cada update dentro de la cabecera
    X-Telegram-Bot-Api-Secret-Token, y es lo que permite distinguir un update
    de Telegram de uno que haya fabricado cualquiera.
    """
    if wait:
        print(f"[Telegram] Esperando {wait}s a que la URL sea accesible...")
        time.sleep(wait)

    webhook_url = f"{public_url.rstrip('/')}{path}"
    params = {"url": webhook_url, "drop_pending_updates": "true"}
    if secret:
        params["secret_token"] = secret

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{bot_token}/setWebhook",
            params=params,
            timeout=10,
        )
        data = resp.json()
    except Exception as e:
        print(f"[Telegram] ADVERTENCIA — no pude registrar el webhook: {e}")
        return False

    if data.get("ok"):
        proteccion = "con secreto" if secret else "SIN SECRETO"
        print(f"[Telegram] Webhook registrado ({proteccion}): {webhook_url}")
        return True

    print(f"[Telegram] ADVERTENCIA — error al registrar webhook: {data}")
    return False


def set_webhook(bot_token: str, public_url: str, secret: str = "") -> None:
    """
    Variante para el modo local: guarda la URL efímera del túnel en .env y luego
    registra. Con un dominio fijo esto no hace falta — ver register_webhook.
    """
    from dotenv import set_key
    set_key(".env", "TELEGRAM_WEBHOOK_URL", public_url)
    print("[Telegram] TELEGRAM_WEBHOOK_URL guardado en .env")
    register_webhook(bot_token, public_url, path="/webhook", secret=secret, wait=5)
