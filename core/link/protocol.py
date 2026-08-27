"""
core/link/protocol.py
Formato de los mensajes entre el servidor de Iris y el agente del portátil.

La conexión la abre SIEMPRE el portátil, hacia fuera. Así no hay que tocar el
router de casa ni exponer ningún puerto, y el mismo socket sirve para que el
servidor le pida cosas al portátil — que es lo que de verdad hace falta.

Como sobre un único socket viajan varias peticiones a la vez, cada una lleva su
`id` y la respuesta lo devuelve. Sin eso no se sabría qué respuesta es de quién.
"""

import uuid

PROTOCOL_VERSION = 1

# Tipos de mensaje
HELLO    = "hello"      # portátil → servidor, al conectar
WELCOME  = "welcome"    # servidor → portátil, si el token es válido
REQUEST  = "request"    # servidor → portátil
RESPONSE = "response"   # portátil → servidor
PING     = "ping"
PONG     = "pong"

# Capacidades que el portátil puede ofrecer
CAP_CLAUDE  = "claude"    # ejecutar claude -p con la suscripción local
CAP_DESKTOP = "desktop"   # ratón, teclado, capturas, lanzar apps


def new_id() -> str:
    return uuid.uuid4().hex


def hello(token: str, capabilities: list[str], name: str = "") -> dict:
    return {
        "type":         HELLO,
        "version":      PROTOCOL_VERSION,
        "token":        token,
        "capabilities": capabilities,
        "name":         name or "portatil",
    }


def welcome(accepted: bool, reason: str = "") -> dict:
    return {"type": WELCOME, "accepted": accepted, "reason": reason}


def request(capability: str, action: str, payload: dict, req_id: str = "") -> dict:
    return {
        "type":       REQUEST,
        "id":         req_id or new_id(),
        "capability": capability,
        "action":     action,
        "payload":    payload or {},
    }


def response_ok(req_id: str, result) -> dict:
    return {"type": RESPONSE, "id": req_id, "ok": True, "result": result}


def response_error(req_id: str, error: str) -> dict:
    return {"type": RESPONSE, "id": req_id, "ok": False, "error": error}


class LinkError(RuntimeError):
    """El agente no respondió, no está conectado, o devolvió un error."""


class AgentUnavailable(LinkError):
    """No hay ningún portátil conectado que ofrezca esa capacidad."""
