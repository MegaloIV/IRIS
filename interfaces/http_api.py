"""
interfaces/http_api.py
La cara HTTP de Iris cuando corre en un servidor.

Tres cosas:
  POST /chat    — un turno de conversación, petición y respuesta
  WS   /stream  — lo mismo pero frase a frase, para que la voz empiece antes
  WS   /agent   — por aquí entra el portátil y ofrece sus capacidades

El webhook de Telegram se monta encima con create_telegram_app().
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from config.settings import settings
from core.link.hub import AgentHub, set_hub

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    message: str
    interface_context: str = ""
    delegate: bool = True
    file_path: str | None = None


class CommandRequest(BaseModel):
    command: str


def create_api(iris) -> FastAPI:
    hub = AgentHub(settings.mode.agent_token)
    set_hub(hub)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Los hilos de Iris (voz, Telegram, proactivo) necesitan este bucle para
        # poder pedirle cosas al portátil desde fuera de asyncio.
        hub.bind_loop(asyncio.get_running_loop())
        yield

    app = FastAPI(title="Iris", lifespan=lifespan)

    # ─── Salud y estado ───────────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/status")
    async def status():
        st = iris.get_status()
        st["agent"] = hub.status()
        return st

    # ─── Conversación ─────────────────────────────────────────────────────────

    @app.post("/chat")
    async def chat(req: ChatRequest):
        loop = asyncio.get_running_loop()
        # A un hilo: IrisAgent es síncrono y una respuesta puede tardar bastante.
        if req.delegate:
            text = await loop.run_in_executor(
                None,
                lambda: iris.delegate_to_claude(
                    req.message, interface_context=req.interface_context
                ),
            )
        else:
            text = await loop.run_in_executor(
                None, lambda: iris.chat(req.message, interface_context=req.interface_context)
            )
        return {"response": text, "mood": iris.personality.state.mood.value}

    @app.post("/command")
    async def command(req: CommandRequest):
        """
        Los comandos corren aquí porque el estado que leen (memoria, gustos,
        trust) solo existe en el servidor.
        """
        from core.commands import handle_command
        loop = asyncio.get_running_loop()

        def _run():
            try:
                return handle_command(req.command, iris)
            except Exception as e:
                # Un comando roto no debe devolver un 500: al otro lado se vería
                # como "no pude alcanzar el servidor", que es falso y despista.
                logger.warning(f"[/command] {req.command}: {type(e).__name__}: {e}")
                return f"[El comando {req.command} falló: {type(e).__name__}: {e}]"

        return {"output": await loop.run_in_executor(None, _run)}

    @app.websocket("/stream")
    async def stream(ws: WebSocket):
        await ws.accept()
        loop = asyncio.get_running_loop()
        stt  = None
        try:
            while True:
                data = await ws.receive_json()
                msg  = data.get("message", "")

                # El portátil puede mandar audio crudo en vez de texto: graba y
                # detecta el silencio allí, pero no carga ningún modelo de voz.
                if not msg and data.get("audio_b64"):
                    import base64
                    if stt is None:
                        from voice.stt import build_stt
                        stt = await loop.run_in_executor(None, build_stt)
                    wav = base64.b64decode(data["audio_b64"])
                    msg = await loop.run_in_executor(None, stt.transcribe_bytes, wav)
                    await ws.send_json({"type": "transcript", "text": msg})

                if not msg:
                    await ws.send_json({"type": "error", "text": "no entendí nada"})
                    continue

                # chat_stream_voice llama a on_sentence desde su propio hilo;
                # cada frase se reenvía al bucle para poder mandarla por el socket.
                def on_sentence(sentence: str):
                    asyncio.run_coroutine_threadsafe(
                        ws.send_json({"type": "sentence", "text": sentence}), loop
                    )

                full = await loop.run_in_executor(
                    None, lambda: iris.chat_stream_voice(msg, on_sentence)
                )
                await ws.send_json({
                    "type": "done",
                    "text": full,
                    "mood": iris.personality.state.mood.value,
                })
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning(f"[/stream] {type(e).__name__}: {e}")

    # ─── El portátil ──────────────────────────────────────────────────────────

    @app.websocket("/agent")
    async def agent(ws: WebSocket):
        await ws.accept()
        conn = await hub.handshake(ws)
        if conn is None:
            await ws.close(code=4001)
            return
        await hub.serve(conn)

    return app
