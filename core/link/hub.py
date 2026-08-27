"""
core/link/hub.py
Lado servidor del enlace: mantiene el agente conectado y le pide cosas.

El problema que resuelve: IrisAgent corre en hilos normales (UI, voz, Telegram,
proactivo) pero el WebSocket vive en el bucle asyncio de FastAPI. Así que hay
dos caminos de entrada — `call()` para código async y `call_sync()` para los
hilos — y el segundo cruza al bucle con run_coroutine_threadsafe.
"""

import asyncio
import logging
import time
from typing import Optional

from core.link import protocol as P

logger = logging.getLogger(__name__)


class _Connection:
    """Un portátil conectado."""

    def __init__(self, ws, capabilities: list[str], name: str):
        self.ws           = ws
        self.capabilities = set(capabilities)
        self.name         = name
        self.connected_at = time.time()
        self.pending: dict[str, asyncio.Future] = {}


class AgentHub:
    """
    Registro del agente conectado y canal de peticiones hacia él.

    Se admite un solo agente a la vez: hay un portátil, y aceptar varios
    obligaría a decidir a cuál mandar cada cosa sin ninguna forma sensata de
    elegir. Una conexión nueva desplaza a la anterior.
    """

    DEFAULT_TIMEOUT = 180.0   # claude -p puede tardar; el companion no

    def __init__(self, token: str):
        self._token = token
        self._conn: Optional[_Connection] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = asyncio.Lock()

    # ─── Estado ───────────────────────────────────────────────────────────────

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Guarda el bucle para que los hilos puedan cruzar hasta él."""
        self._loop = loop

    def has(self, capability: str) -> bool:
        return bool(self._conn and capability in self._conn.capabilities)

    def status(self) -> dict:
        if not self._conn:
            return {"connected": False}
        return {
            "connected":    True,
            "name":         self._conn.name,
            "capabilities": sorted(self._conn.capabilities),
            "uptime_s":     round(time.time() - self._conn.connected_at),
            "pending":      len(self._conn.pending),
        }

    # ─── Ciclo de vida de la conexión ─────────────────────────────────────────

    async def handshake(self, ws) -> Optional[_Connection]:
        """Valida el token y registra el agente. None si se rechaza."""
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
        except Exception:
            return None

        if msg.get("type") != P.HELLO or msg.get("token") != self._token:
            await ws.send_json(P.welcome(False, "token invalido"))
            logger.warning("[Hub] Conexión rechazada: token inválido.")
            return None

        conn = _Connection(ws, msg.get("capabilities", []), msg.get("name", "portatil"))

        async with self._lock:
            if self._conn is not None:
                # Un agente nuevo desplaza al anterior; sus peticiones en vuelo
                # ya no van a recibir respuesta nunca, así que se cancelan aquí
                # en vez de dejarlas colgadas hasta el timeout.
                self._fail_pending(self._conn, "reemplazado por otra conexión")
            self._conn = conn

        await ws.send_json(P.welcome(True))
        logger.info(f"[Hub] Agente '{conn.name}' conectado — {sorted(conn.capabilities)}")
        return conn

    async def serve(self, conn: _Connection) -> None:
        """Lee respuestas del agente hasta que se corte la conexión."""
        try:
            while True:
                msg = await conn.ws.receive_json()
                mtype = msg.get("type")

                if mtype == P.RESPONSE:
                    fut = conn.pending.pop(msg.get("id", ""), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif mtype == P.PING:
                    await conn.ws.send_json({"type": P.PONG})
        except Exception as e:
            logger.info(f"[Hub] Agente '{conn.name}' desconectado: {type(e).__name__}")
        finally:
            async with self._lock:
                if self._conn is conn:
                    self._conn = None
            self._fail_pending(conn, "el agente se desconectó")

    @staticmethod
    def _fail_pending(conn: _Connection, reason: str) -> None:
        for fut in conn.pending.values():
            if not fut.done():
                fut.set_exception(P.LinkError(reason))
        conn.pending.clear()

    # ─── Peticiones ───────────────────────────────────────────────────────────

    async def call(self, capability: str, action: str, payload: dict,
                   timeout: float = None) -> dict:
        conn = self._conn
        if conn is None or capability not in conn.capabilities:
            raise P.AgentUnavailable(
                f"no hay ningún agente conectado que ofrezca '{capability}'"
            )

        msg = P.request(capability, action, payload)
        fut = asyncio.get_running_loop().create_future()
        conn.pending[msg["id"]] = fut

        try:
            await conn.ws.send_json(msg)
            return await asyncio.wait_for(fut, timeout or self.DEFAULT_TIMEOUT)
        except asyncio.TimeoutError:
            raise P.LinkError(f"el agente no respondió en {timeout or self.DEFAULT_TIMEOUT:.0f}s")
        finally:
            conn.pending.pop(msg["id"], None)

    def call_sync(self, capability: str, action: str, payload: dict,
                  timeout: float = None) -> dict:
        """
        Igual que call(), pero desde un hilo normal.

        IrisAgent no es async: lo llaman el hilo de la UI, el de voz, el motor
        proactivo y el ejecutor de Telegram. Este método cruza hasta el bucle
        de FastAPI y espera el resultado.
        """
        if self._loop is None:
            raise P.LinkError("el hub no está asociado a ningún bucle todavía")

        fut = asyncio.run_coroutine_threadsafe(
            self.call(capability, action, payload, timeout), self._loop
        )
        # Un poco más que el timeout interno: si salta el de dentro queremos su
        # mensaje, que es más explicativo que un TimeoutError pelado.
        return fut.result((timeout or self.DEFAULT_TIMEOUT) + 15)


# Instancia única — la crea el arranque del servidor
_hub: Optional[AgentHub] = None


def get_hub() -> Optional[AgentHub]:
    return _hub


def set_hub(hub: AgentHub) -> None:
    global _hub
    _hub = hub
