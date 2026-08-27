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

def run_claude(
    prompt: str,
    file_path: str = None,
    system_prompt: str = "",
    resume_session: str = "",
    json_schema: dict = None,
):
    """
    Ejecuta Claude donde toque y devuelve un ClaudeResult.

    El coste se apunta aquí y solo aquí: es el punto por el que pasan los dos
    modos, así que ni se pierde en remoto ni se cuenta dos veces en local.
    """
    from core.claude_delegate import ClaudeResult, ledger

    if settings.mode.mode == "server":
        payload = _remote_call(
            P.CAP_CLAUDE, "run",
            {
                "prompt": prompt, "file_path": file_path,
                "system_prompt": system_prompt, "resume_session": resume_session,
                "json_schema": json_schema,
            },
            timeout=float(ClaudeTimeout()),
        )
        result = ClaudeResult.from_dict(payload)
    else:
        from core.claude_delegate import ClaudeDelegator
        result = ClaudeDelegator().run_sync(
            prompt, file_path,
            system_prompt=system_prompt,
            resume_session=resume_session,
            json_schema=json_schema,
        )

    ledger.record(result)
    return result


def stream_claude(
    prompt: str,
    file_path: str = None,
    system_prompt: str = "",
    resume_session: str = "",
    on_text=None,
):
    """
    Como run_claude, pero entregando el texto según se genera.

    En modo servidor no hay streaming: el enlace con el portátil es
    petición/respuesta, no un canal continuo, así que se cae a la llamada
    bloqueante y el texto se entrega de una vez al final. Se nota en el ritmo,
    no en el resultado — y evita fingir un streaming que no existe.
    """
    from core.claude_delegate import ClaudeDelegator, ledger

    if settings.mode.mode == "server":
        result = run_claude(prompt, file_path, system_prompt, resume_session)
        if on_text and result.ok and result.text:
            on_text(result.text)
        return result

    result = ClaudeDelegator().run_stream(
        prompt, file_path,
        system_prompt=system_prompt,
        resume_session=resume_session,
        on_text=on_text,
    )
    ledger.record(result)
    return result


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
        result = ClaudeDelegator().run_sync(
            payload.get("prompt", ""),
            payload.get("file_path"),
            system_prompt=payload.get("system_prompt", ""),
            resume_session=payload.get("resume_session", ""),
            json_schema=payload.get("json_schema"),
        )
        return result.to_dict()

    def desktop_http(payload: dict) -> dict:
        url    = settings.companion.url
        path   = payload.get("path", "/")
        method = payload.get("method", "GET").upper()
        resp = (companion_get(url, path, timeout=15) if method == "GET"
                else companion_post(url, path, payload.get("payload") or {}, timeout=15))
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {}

    return {
        (P.CAP_CLAUDE,  "run"):  claude_run,
        (P.CAP_DESKTOP, "http"): desktop_http,
    }
