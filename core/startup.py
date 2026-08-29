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
        set_webhook(settings.telegram.bot_token, public_url, settings.telegram.webhook_secret)
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


def setup_agent_link():
    """
    Modo cliente: abre el WebSocket hacia el servidor y ofrece lo que solo se
    puede hacer aquí — Claude con tu suscripción, y el escritorio.
    Retorna el AgentClient o None.
    """
    if settings.mode.mode != "client":
        return None
    try:
        from core.link.agent import AgentClient
        from core.executor import build_local_handlers

        client = AgentClient(
            settings.mode.server_url,
            settings.mode.agent_token,
            settings.mode.agent_name,
        )
        for (cap, action), handler in build_local_handlers().items():
            client.register(cap, action, handler)
        client.start()
        print(f"[Agente] Conectando a {settings.mode.server_url} — ofrezco {client.capabilities}")
        return client
    except Exception as e:
        print(f"[Agente] No se pudo iniciar el enlace: {e}")
        return None


def _register_server_webhook() -> None:
    """
    Apunta el webhook de Telegram a este servidor.

    En modo local lo hacía `setup_telegram()` tras levantar el túnel, porque la
    URL de cloudflared cambiaba en cada arranque. En servidor el dominio es fijo
    y nadie registraba nada: la app quedaba montada en /tg escuchando updates
    que Telegram seguía mandando a la última URL efímera que conociera. Es decir,
    el bot dejaba de responder al desplegar, sin ningún error que lo dijera.
    """
    from scripts.start_telegram import register_webhook

    public_url = settings.server.public_url
    if not public_url:
        print(
            "[Telegram] No hay IRIS_DOMAIN ni IRIS_PUBLIC_URL — el webhook no se "
            "registra y el bot no recibirá nada. Defínelos en el .env del servidor."
        )
        return

    register_webhook(
        settings.telegram.bot_token,
        public_url,
        path="/tg/webhook",
        secret=settings.telegram.webhook_secret,
        wait=3,
    )


def run_api_server(iris):
    """Modo servidor: levanta FastAPI con /chat, /stream y /agent. Bloquea."""
    import uvicorn
    from interfaces.http_api import create_api

    app = create_api(iris)

    # El editor del cerebro, con llave. Se monta aquí y no en su propio puerto
    # porque así hereda el TLS de Caddy — y sin HTTPS mandar el token por la URL
    # sería regalarlo a cualquiera que mire el tráfico.
    if settings.brain.token:
        try:
            from interfaces.brain_api import create_brain_api
            app.mount("/cerebro", create_brain_api(iris, exigir_token=True))
            print("[Cerebro] Editor montado en /cerebro (requiere token)")
        except Exception as e:
            print(f"[Cerebro] No se pudo montar el editor: {e}")
    else:
        print("[Cerebro] Sin BRAIN_TOKEN — el editor no se expone.")

    if settings.telegram.enabled and settings.telegram.bot_token:
        try:
            from interfaces.telegram_bot import create_telegram_app
            app.mount("/tg", create_telegram_app(iris))
            print("[Telegram] Webhook montado en /tg/webhook")
            # En un hilo y con margen: uvicorn todavía no escucha, y registrar
            # antes haría que el primer update de Telegram se topara con la
            # puerta cerrada. Reintenta solo, pero el síntoma —"no contesta al
            # primer mensaje"— es de los que hacen perder una tarde.
            threading.Thread(
                target=_register_server_webhook, daemon=True, name="iris-tg-webhook",
            ).start()
        except Exception as e:
            print(f"[Telegram] No se pudo montar: {e}")

    print(f"[Servidor] Iris escuchando en {settings.server.host}:{settings.server.port}")
    uvicorn.run(app, host=settings.server.host, port=settings.server.port, log_level="warning")


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
