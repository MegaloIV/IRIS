"""
core/startup.py
Inicialización de subsistemas de Iris: Telegram, proactive engine, voz.
"""

import threading
import logging

from config.settings import settings


def _run_telegram_server(iris) -> None:
    import asyncio
    import uvicorn
    from interfaces.telegram_bot import create_telegram_app

    app  = create_telegram_app(iris)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    config = uvicorn.Config(
        app,
        host=settings.server.host,
        port=settings.server.port,
        loop="none",
        log_level="warning",
    )
    loop.run_until_complete(uvicorn.Server(config).serve())


def telegram_send_sync(token: str, chat_id: int, text: str) -> bool:
    import requests
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        return r.ok
    except Exception as e:
        logging.warning(f"[Proactive] Telegram send falló: {e}")
        return False


def setup_telegram(iris):
    """Inicia cloudflared + webhook + servidor FastAPI. Retorna (cf_proc, active)."""
    if not (settings.telegram.enabled and settings.telegram.bot_token):
        return None, False
    try:
        from scripts.start_telegram import start_cloudflared, set_webhook
        cf_proc, public_url = start_cloudflared(settings.server.port)
        print(f"[cloudflared] URL pública: {public_url}")
        set_webhook(settings.telegram.bot_token, public_url)
        threading.Thread(target=_run_telegram_server, args=(iris,), daemon=True).start()
        print("[Telegram] Bot activo — envía un mensaje al bot para empezar.")
        return cf_proc, True
    except Exception as e:
        print(f"[Telegram] Error al iniciar: {e}")
        print("[Telegram] Continuando sin Telegram.")
        return None, False


def setup_proactive(iris, ui_signals, telegram_active: bool):
    """Inicia el motor proactivo. Retorna el engine o None."""
    try:
        from core.proactive import ProactiveEngine
        from config.prompts import TELEGRAM_INTERFACE_ADDON

        def _proactive_send(text: str):
            if (
                telegram_active
                and settings.telegram.bot_token
                and settings.telegram.owner_id
            ):
                sent = telegram_send_sync(
                    settings.telegram.bot_token,
                    settings.telegram.owner_id,
                    text,
                )
                if sent:
                    return
            ui_signals.text_updated.emit(text)
            if iris._voice:
                iris.speak(text)

        iface_ctx = TELEGRAM_INTERFACE_ADDON if telegram_active else ""
        engine = ProactiveEngine(iris, _proactive_send, interface_context=iface_ctx)
        engine.start()
        return engine
    except Exception as e:
        print(f"[Proactive] No se pudo iniciar el motor proactivo: {e}")
        return None


def setup_voice(iris, ui_signals):
    """Inicia el subsistema de voz."""
    print("[Voice] Iniciando sistema de voz...")
    try:
        iris.start_voice(
            on_speaking_sentence=ui_signals.text_updated.emit,
            on_listening_changed=ui_signals.listening_changed.emit,
        )
        print("[Voice] Listo — presiona el botón Copilot para hablar con Iris.")
    except Exception as e:
        print(f"[Voice] Error: {e}")
        print("[Voice] Continuando en modo texto.")
