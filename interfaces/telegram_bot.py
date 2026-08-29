"""
interfaces/telegram_bot.py
Telegram interface for Iris — receives messages via FastAPI webhook and
processes them through the same IrisAgent pipeline as the desktop UI.

Only responds to TELEGRAM_OWNER_ID; all other senders are silently ignored.
"""

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request, Response
from telegram import Bot, Update
from telegram.constants import ChatAction

from config.settings import settings
from config.prompts import TELEGRAM_INTERFACE_ADDON, TELEGRAM_VOICE_OPTION
from core.commands import handle_command, is_command

logger = logging.getLogger(__name__)

_MAX_LEN = 4096
_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
_TYPING_INTERVAL = 4       # seconds — Telegram's indicator expires after 5s
_MSG_DELAY = 0.8           # seconds between split messages
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?…])\s+')


def _split_into_messages(text: str) -> list[str]:
    """Split into short chat-style messages: max 2 sentences per message.
    Paragraph breaks always produce a new message. Hard-caps at 4096 chars."""
    paragraphs = [p.strip() for p in re.split(r'\n\n+', text) if p.strip()]
    messages: list[str] = []
    for para in paragraphs:
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(para) if s.strip()]
        if not sentences:
            continue
        for i in range(0, len(sentences), 2):
            chunk = " ".join(sentences[i:i + 2])
            while len(chunk) > _MAX_LEN:
                messages.append(chunk[:_MAX_LEN])
                chunk = chunk[_MAX_LEN:]
            if chunk:
                messages.append(chunk)
    return messages or [text]


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

    secret = settings.telegram.webhook_secret
    if not secret:
        logger.warning(
            "[Telegram] Sin secreto de webhook — cualquiera que dé con la URL "
            "podría hacerse pasar por ti. Define TELEGRAM_WEBHOOK_SECRET."
        )

    bot = Bot(token=bot_token)
    app = FastAPI()

    # Optional TTS — synthesises audio for voice messages
    tts = None
    if settings.telegram.tts_enabled:
        try:
            from voice.tts import TTSEngine
            tts = TTSEngine()
            if not tts.ffmpeg_available:
                logger.warning(
                    "[Telegram] TTS habilitado pero ffmpeg no está instalado — "
                    "las respuestas se enviarán como texto. "
                    "Instala ffmpeg para activar audio: winget install ffmpeg"
                )
                tts = None
            else:
                logger.info("[Telegram] TTS activo — se enviarán mensajes de voz")
        except Exception as e:
            logger.warning(f"[Telegram] TTS no disponible, usando solo texto: {e}")

    # Optional STT — transcribes incoming voice notes
    stt = None
    if settings.telegram.stt_enabled:
        try:
            # build_stt(), no STTEngine(): en modo servidor elige la API de Groq.
            # Instanciar el motor local aquí cargaba faster-whisper, que ni
            # siquiera está en requirements.server.txt — así que en el servidor
            # fallaba y las notas de voz se descartaban diciendo "STT desactivado".
            from voice.stt import build_stt
            stt = build_stt()
            logger.info(f"[Telegram] STT activo ({type(stt).__name__}) — se transcribirán las notas de voz")
        except Exception as e:
            logger.warning(f"[Telegram] STT no disponible: {e}")

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
        # Telegram devuelve en esta cabecera el secreto que le dimos al registrar
        # el webhook. Es lo único que distingue un update suyo de uno fabricado:
        # el owner_id viaja DENTRO del cuerpo, así que quien llama se lo inventa.
        if secret and request.headers.get(_SECRET_HEADER) != secret:
            logger.warning("[Telegram] Update rechazado — secreto del webhook incorrecto")
            return Response(status_code=403)

        try:
            data   = await request.json()
            update = Update.de_json(data, bot)
        except Exception as e:
            logger.error(f"[Telegram] Error parseando update: {e}")
            return Response(status_code=200)

        message = update.message or update.edited_message
        if not message:
            return Response(status_code=200)

        chat_id = message.chat_id
        user_id = message.from_user.id if message.from_user else None

        if owner_id and user_id != owner_id:
            logger.warning(f"[Telegram] Mensaje ignorado — user_id={user_id} no es el owner")
            return Response(status_code=200)

        # Resolve user_input and download any attached file to a temp path
        temp_file_path: str | None = None

        if message.photo:
            tg_file = await bot.get_file(message.photo[-1].file_id)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
            tmp.close()
            await tg_file.download_to_drive(tmp.name)
            temp_file_path = tmp.name
            user_input = (message.caption or "mira esto").strip()
            logger.info(f"[Telegram] ← [imagen] {user_input[:80]}")
        elif message.document:
            doc    = message.document
            suffix = Path(doc.file_name).suffix if doc.file_name else ""
            tg_file = await bot.get_file(doc.file_id)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.close()
            await tg_file.download_to_drive(tmp.name)
            temp_file_path = tmp.name
            user_input = (message.caption or "mira esto").strip()
            logger.info(f"[Telegram] ← [archivo: {doc.file_name}] {user_input[:80]}")
        elif message.voice or message.audio:
            media   = message.voice or message.audio
            suffix  = ".ogg" if message.voice else (Path(media.file_name).suffix if getattr(media, "file_name", None) else ".ogg")
            tg_file = await bot.get_file(media.file_id)
            tmp     = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            tmp.close()
            await tg_file.download_to_drive(tmp.name)
            if stt:
                transcribed = stt.transcribe_file(tmp.name)
                os.unlink(tmp.name)
                if not transcribed:
                    logger.warning("[Telegram] STT no obtuvo texto del audio")
                    return Response(status_code=200)
                user_input = transcribed
                logger.info(f"[Telegram] ← [voz→texto] {user_input[:100]}")
            else:
                os.unlink(tmp.name)
                logger.info("[Telegram] Audio recibido pero STT desactivado — ignorando")
                return Response(status_code=200)
        elif message.text:
            user_input = message.text.strip()
            logger.info(f"[Telegram] ← {user_input[:100]}")
        else:
            return Response(status_code=200)

        loop = asyncio.get_event_loop()

        # Los comandos son los mismos que en la terminal. Antes llegaban a Iris
        # como texto suelto, así que /gustos o /coste desde el móvil producían
        # una respuesta conversacional en vez del dato. Sin apagado remoto: un
        # /salir escrito sin querer no debe tumbar el proceso.
        # Solo texto suelto: un pie de foto que empiece por barra es un pie de
        # foto, y además saldríamos de aquí sin borrar el archivo temporal.
        if temp_file_path is None and is_command(user_input):
            salida = await loop.run_in_executor(
                None, lambda: handle_command(user_input, iris, allow_shutdown=False),
            )
            for parte in _split_into_messages(salida):
                await bot.send_message(chat_id=chat_id, text=parte)
            logger.info(f"[Telegram] → [comando {user_input.split()[0]}]")
            return Response(status_code=200)

        stop_evt = asyncio.Event()
        typing   = asyncio.create_task(_keep_typing(chat_id, stop_evt))

        iface_ctx = TELEGRAM_INTERFACE_ADDON + (TELEGRAM_VOICE_OPTION if tts else "")
        try:
            response_text = await loop.run_in_executor(
                None,
                lambda: iris.delegate_to_claude(
                    user_input,
                    file_path=temp_file_path,
                    interface_context=iface_ctx,
                ),
            )
        except Exception as e:
            logger.error(f"[Telegram] Error en IrisAgent: {e}")
            response_text = f"[Error interno: {e}]"
        finally:
            stop_evt.set()
            typing.cancel()
            if temp_file_path:
                try:
                    os.unlink(temp_file_path)
                except Exception:
                    pass

        # Parse optional [VOZ] tag — Iris decides to send voice
        wants_voice = response_text.startswith("[VOZ]")
        if wants_voice:
            response_text = response_text[5:].strip()

        logger.info(f"[Telegram] → {'[VOZ] ' if wants_voice else ''}{response_text[:100]}")

        if wants_voice and tts:
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

        if wants_voice:
            # Pidió voz y no hubo voz. Lo que ella escribió da por hecho que el
            # audio llega ("aquí tienes el audio"), así que mandarlo tal cual es
            # decirle al usuario algo que no es verdad: ve un mensaje anunciando
            # un audio que nunca aparece. Mejor que lo sepa.
            logger.warning("[Telegram] Se pidió voz pero no se pudo sintetizar; avisando por texto.")
            response_text = (
                f"{response_text}\n\n"
                "(quería mandártelo en audio pero mi voz no está disponible ahora mismo)"
            )

        # Send as text (default, or fallback if TTS failed)
        parts = _split_into_messages(response_text)
        for i, part in enumerate(parts):
            await bot.send_message(chat_id=chat_id, text=part)
            if i < len(parts) - 1:
                await asyncio.sleep(_MSG_DELAY)

        return Response(status_code=200)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "iris": "active"}

    return app
