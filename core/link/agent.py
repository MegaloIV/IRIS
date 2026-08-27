"""
core/link/agent.py
Lado portátil del enlace: se conecta al servidor y atiende lo que le pida.

Corre en su propio hilo con su propio bucle asyncio, para no interferir con Qt
ni con el resto de hilos de Iris. Reconecta solo con backoff — un portátil se
suspende, cambia de wifi y se queda sin red, y ninguna de esas cosas debería
requerir reiniciar nada.
"""

import asyncio
import json
import logging
import threading
from typing import Callable, Optional

import websockets

from core.link import protocol as P

logger = logging.getLogger(__name__)


class AgentClient:
    """
    Cliente que ofrece capacidades locales al servidor de Iris.

    Uso:
        client = AgentClient(url, token)
        client.register("claude", "run", lambda payload: {...})
        client.start()
    """

    RECONNECT_MIN = 2.0
    RECONNECT_MAX = 60.0

    def __init__(self, server_url: str, token: str, name: str = "portatil"):
        self._url    = self._ws_url(server_url)
        self._token  = token
        self._name   = name
        self._handlers: dict[tuple[str, str], Callable[[dict], dict]] = {}
        self._thread: Optional[threading.Thread] = None
        self._loop:   Optional[asyncio.AbstractEventLoop] = None
        self._ws     = None
        self._tasks: set = set()
        self._stop   = threading.Event()
        self.connected = False

    @staticmethod
    def _ws_url(base: str) -> str:
        u = base.rstrip("/")
        if u.startswith("https://"): u = "wss://" + u[8:]
        elif u.startswith("http://"): u = "ws://" + u[7:]
        return u + "/agent"

    # ─── Registro de capacidades ──────────────────────────────────────────────

    def register(self, capability: str, action: str, handler: Callable[[dict], dict]) -> None:
        """El handler es síncrono: corre en un hilo aparte para no bloquear el socket."""
        self._handlers[(capability, action)] = handler

    @property
    def capabilities(self) -> list[str]:
        return sorted({cap for cap, _ in self._handlers})

    # ─── Ciclo de vida ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="iris-agent-link")
        self._thread.start()

    def stop(self) -> None:
        """
        Cierra el enlace.

        No basta con poner la bandera: el bucle está parado en `await ws.recv()`
        y no la mira hasta que llegue un mensaje, que podría no llegar nunca.
        Hay que cerrar el socket para desbloquearlo — y así el servidor se
        entera en el momento, no cuando expire algún timeout.
        """
        self._stop.set()

        loop, ws = self._loop, self._ws
        if loop is None or loop.is_closed():
            return          # ya se detuvo solo (p. ej. token rechazado)

        async def _close():
            for t in list(self._tasks):
                t.cancel()
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass

        try:
            asyncio.run_coroutine_threadsafe(_close(), loop).result(timeout=5)
        except Exception:
            pass

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._reconnect_forever())
        finally:
            self._loop.close()

    async def _reconnect_forever(self) -> None:
        delay = self.RECONNECT_MIN
        while not self._stop.is_set():
            try:
                await self._session()
                delay = self.RECONNECT_MIN      # una sesión buena resetea el backoff
            except Exception as e:
                logger.info(f"[Agente] Sin conexión ({type(e).__name__}); reintento en {delay:.0f}s")
            finally:
                self.connected = False
                self._ws = None

            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.RECONNECT_MAX)

    async def _session(self) -> None:
        async with websockets.connect(self._url, max_size=32 * 1024 * 1024) as ws:
            self._ws = ws
            await ws.send(json.dumps(P.hello(self._token, self.capabilities, self._name)))

            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if not reply.get("accepted"):
                logger.error(f"[Agente] Rechazado: {reply.get('reason', '?')}")
                self._stop.set()          # token malo: reintentar no va a arreglarlo
                return

            self.connected = True
            logger.info(f"[Agente] Conectado — ofrezco {self.capabilities}")

            while not self._stop.is_set():
                msg = json.loads(await ws.recv())
                if msg.get("type") == P.REQUEST:
                    task = asyncio.create_task(self._handle(ws, msg))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                elif msg.get("type") == P.PING:
                    await ws.send(json.dumps({"type": P.PONG}))

    # ─── Ejecución ────────────────────────────────────────────────────────────

    async def _handle(self, ws, msg: dict) -> None:
        req_id  = msg.get("id", "")
        key     = (msg.get("capability", ""), msg.get("action", ""))
        handler = self._handlers.get(key)

        if handler is None:
            await ws.send(json.dumps(P.response_error(req_id, f"no sé hacer {key}")))
            return

        try:
            # A un hilo: los handlers son síncronos y algunos tardan minutos
            # (claude -p). Si corrieran aquí bloquearían el socket entero y las
            # demás peticiones se quedarían esperando.
            result = await asyncio.to_thread(handler, msg.get("payload", {}))
            await self._reply(ws, P.response_ok(req_id, result))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[Agente] Error en {key}: {e}")
            await self._reply(ws, P.response_error(req_id, f"{type(e).__name__}: {e}"))

    @staticmethod
    async def _reply(ws, payload: dict) -> None:
        """Un handler lento puede terminar cuando la conexión ya se cerró."""
        try:
            await ws.send(json.dumps(payload))
        except Exception as e:
            logger.debug(f"[Agente] No pude responder, conexión cerrada: {type(e).__name__}")
