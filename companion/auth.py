"""
companion/auth.py
Token compartido entre Iris (WSL2) y el companion (Windows nativo).

El companion no puede escuchar solo en loopback: Iris vive en WSL2 y llega a
Windows por la NIC virtual, no por 127.0.0.1. Así que el puerto tiene que estar
abierto en la red del host, y lo que impide que cualquiera de la LAN mueva tu
ratón es este token, no el bind.

Los dos procesos corren en intérpretes distintos y no comparten paquete, así que
el canal es un archivo junto a este módulo: el companion lo crea al arrancar si
no existe, e Iris lo lee. Nunca se versiona.
"""

import secrets
from pathlib import Path

TOKEN_FILE  = Path(__file__).parent / ".iris_token"
TOKEN_HEADER = "X-Iris-Token"


def load_or_create_token() -> str:
    """Lee el token; lo genera la primera vez. Lo llama el companion."""
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token

    token = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:  # en Windows es un no-op, pero no molesta
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return token


def read_token() -> str:
    """Lee el token sin crearlo. Lo llama Iris; '' si el companion no ha arrancado nunca."""
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
