"""
interfaces/telegram_bot.py
Telegram interface for Iris — receives messages via FastAPI webhook and
processes them through the same IrisAgent pipeline as the desktop UI.

Only responds to TELEGRAM_OWNER_ID; all other senders are silently ignored.
"""

import asyncio
import logging
import os

from fastapi import FastAPI, Request, Response
from telegram import Bot, Update
from telegram.constants import ChatAction

from config.settings import settings
from config.prompts import TELEGRAM_INTERFACE_ADDON

logger = logging.getLogger(__name__)

_MAX_LEN = 4096
_TYPING_INTERVAL = 4  # seconds — Telegram's indicator expires after 5s


def _split_text(text: str) -> list[str]:
    """Split at the last newline before 4096 chars; falls back to hard split."""
    if len(text) <= _MAX_LEN:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= _MAX_LEN:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, _MAX_LEN)
        if split_at <= 0:
            split_at = _MAX_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def create_telegram_app(iris) -> FastAPI:
    """
    Build and return the FastAPI app for the Telegram webhook.

    iris: an already-initialised IrisAgent — shared with the desktop UI so
          personality, memory, and mood state are the same across interfaces.
    """
    bot_token = settings.telegram.bot_token
    owner_id  = settings.telegram.owner_id  # int | None

    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN no está configurado en .env")

    bot = Bot(token=bot_token)
    app = FastAPI()

    # Optional TTS — synthesises audio for voice messages
    tts = None
    if settings.telegram.tts_enabled:
        try:
            from voice.tts import TTSEngine
            tts = TTSEngine()
            logger.info("[Telegram] TTS activo — se enviarán mensajes de voz")
        except Exception as e:
            logger.warning(f"[Telegram] TTS no disponible, usando solo texto: {e}")

    async def _keep_typing(chat_id: int, stop: asyncio.Event) -> None:
        """Refresh the typing indicator every _TYPING_INTERVAL seconds."""
        while not stop.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(_TYPING_INTERVAL)

    @app.post("/webhook")
    async def webhook(request: Request) -> Response:
        try:
            data   = await request.json()
            update = Update.de_json(data, bot)
        except Exception as e:
            logger.error(f"[Telegram] Error parseando update: {e}")
            return Response(status_code=200)

        message = update.message or update.edited_message
        if not message or not message.text:
            return Response(status_code=200)

        chat_id = message.chat_id
        user_id = message.from_user.id if message.from_user else None

        if owner_id and user_id != owner_id:
            logger.warning(f"[Telegram] Mensaje ignorado — user_id={user_id} no es el owner")
            return Response(status_code=200)

        user_input = message.text.strip()
        logger.info(f"[Telegram] ← {user_input[:100]}")

        loop     = asyncio.get_event_loop()
        stop_evt = asyncio.Event()
        typing   = asyncio.create_task(_keep_typing(chat_id, stop_evt))

        try:
            response_text = await loop.run_in_executor(
                None,
                lambda: iris.delegate_to_claude(
                    user_input,
                    interface_context=TELEGRAM_INTERFACE_ADDON,
                ),
            )
        except Exception as e:
            logger.error(f"[Telegram] Error en IrisAgent: {e}")
            response_text = f"[Error interno: {e}]"
        finally:
            stop_evt.set()
            typing.cancel()

        logger.info(f"[Telegram] → {response_text[:100]}")

        # Prefer voice when TTS is enabled
        if tts:
            try:
                ogg_path = await loop.run_in_executor(
                    None, tts.synthesize_for_telegram, response_text
                )
                if ogg_path:
                    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VOICE)
                    with open(ogg_path, "rb") as f:
                        await bot.send_voice(chat_id=chat_id, voice=f)
                    os.unlink(ogg_path)
                    return Response(status_code=200)
            except Exception as e:
                logger.warning(f"[Telegram] TTS falló, enviando texto: {e}")

        # Text fallback — split if over 4096 chars
        for chunk in _split_text(response_text):
            await bot.send_message(chat_id=chat_id, text=chunk)

        return Response(status_code=200)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "iris": "active"}

    return app
