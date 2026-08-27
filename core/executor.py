"""
core/executor.py
Dónde se ejecutan las cosas que necesitan estar en el portátil.

Iris llama a `run_claude()` y `desktop_request()` sin saber dónde acaban. En
modo `local` corren aquí mismo, como siempre. En modo `server` viajan por el
WebSocket hasta el portátil, que tiene la suscripción de Claude, el ratón y la
pantalla.

Los sitios que llaman no cambian: por eso la lógica de decidir vive aquí y no
repartida por agent.py y claude_delegate.py.
"""

import logging

from config.settings import settings
from core.link import protocol as P

logger = logging.getLogger(__name__)


def _remote_call(capability: str, action: str, payload: dict, timeout: float = None) -> dict:
    from core.link.hub import get_hub
    hub = get_hub()
    if hub is None:
        raise P.AgentUnavailable("el enlace con el portátil no está iniciado")
    reply = hub.call_sync(capability, action, payload, timeout)
    if not reply.get("ok"):
        raise P.LinkError(reply.get("error", "error sin detalle"))
    return reply.get("result") or {}


def agent_available(capability: str) -> bool:
    """¿Puede Iris hacer esto ahora mismo? En local, siempre."""
    if settings.mode.mode != "server":
        return True
    from core.link.hub import get_hub
    hub = get_hub()
    return bool(hub and hub.has(capability))


# ─── Claude ───────────────────────────────────────────────────────────────────

def run_claude(prompt: str, file_path: str = None) -> str:
    if settings.mode.mode == "server":
        result = _remote_call(
            P.CAP_CLAUDE, "run",
            {"prompt": prompt, "file_path": file_path},
            timeout=float(ClaudeTimeout()),
        )
        return result.get("output", "")

    from core.claude_delegate import ClaudeDelegator
    return ClaudeDelegator().run_sync(prompt, file_path)


def ClaudeTimeout() -> int:
    from core.claude_delegate import ClaudeDelegator
    return ClaudeDelegator.TIMEOUT_SECONDS + 30


# ─── Escritorio ───────────────────────────────────────────────────────────────

def desktop_request(method: str, path: str, payload: dict = None, timeout: float = 15.0):
    """Una llamada al companion, esté donde esté el companion."""
    if settings.mode.mode == "server":
        return _remote_call(
            P.CAP_DESKTOP, "http",
            {"method": method, "path": path, "payload": payload or {}},
            timeout=timeout,
        )

    from core.claude_delegate import companion_get, companion_post
    url = settings.companion.url
    resp = (companion_get(url, path, timeout=int(timeout)) if method == "GET"
            else companion_post(url, path, payload or {}, timeout=int(timeout)))
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {}


# ─── Handlers del lado portátil ───────────────────────────────────────────────

def build_local_handlers() -> dict:
    """
    Lo que el portátil sabe hacer. Se registra en el AgentClient cuando
    IRIS_MODE=client, y es la contraparte de las dos funciones de arriba.
    """
    from core.claude_delegate import ClaudeDelegator, companion_get, companion_post

    def claude_run(payload: dict) -> dict:
        out = ClaudeDelegator().run_sync(payload.get("prompt", ""), payload.get("file_path"))
        return {"output": out}

    def desktop_http(payload: dict) -> dict:
        url    = settings.companion.url
        path   = payload.get("path", "/")
        method = payload.get("method", "GET").upper()
        resp = (companion_get(url, path, timeout=15) if method == "GET"
                else companion_post(url, path, payload.get("payload") or {}, timeout=15))
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:
            return {}

        # La captura se lee AQUÍ, en el portátil, y viaja en base64. El servidor
        # no puede abrir una ruta de este disco.
        if path == "/screenshot" and isinstance(data, dict):
            from core.claude_delegate import _read_screenshot_b64
            data["image_b64"] = _read_screenshot_b64(data)
        return data

    return {
        (P.CAP_CLAUDE,  "run"):  claude_run,
        (P.CAP_DESKTOP, "http"): desktop_http,
    }
