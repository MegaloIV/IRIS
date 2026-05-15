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


def set_webhook(bot_token: str, public_url: str) -> None:
    """Persist the public URL to .env, then register the webhook with Telegram."""
    import time
    from dotenv import set_key
    set_key(".env", "TELEGRAM_WEBHOOK_URL", public_url)
    print("[Telegram] TELEGRAM_WEBHOOK_URL guardado en .env")
    print("[Telegram] Esperando 5s para que el túnel sea accesible...")
    time.sleep(5)

    webhook_url = f"{public_url}/webhook"
    resp = requests.get(
        f"https://api.telegram.org/bot{bot_token}/setWebhook",
        params={"url": webhook_url},
        timeout=10,
    )
    data = resp.json()
    if data.get("ok"):
        print(f"[Telegram] Webhook registrado: {webhook_url}")
    else:
        print(f"[Telegram] ADVERTENCIA — error al registrar webhook: {data}")
