"""
companion/server.py
Servidor HTTP de control de escritorio para Iris.

Corre en Python NATIVO de Windows (no WSL2).
Iris lo llama desde WSL2 vía HTTP a localhost:7891.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import uvicorn

from auth import TOKEN_HEADER, load_or_create_token
from screen import take_screenshot, get_ui_elements
from control import (
    click, double_click, right_click, move, drag,
    type_text, press_key, hotkey, scroll, launch_app, get_windows, get_installed_apps,
)

app  = FastAPI(title="Iris Desktop Companion")
PORT = 7891

TOKEN = load_or_create_token()

# Todo lo que hay debajo mueve el ratón, escribe con el teclado, captura la
# pantalla o lanza programas. Sin esto, cualquiera en la red hace lo mismo.
_OPEN_PATHS = {"/health"}


@app.middleware("http")
async def require_token(request: Request, call_next):
    if request.url.path not in _OPEN_PATHS:
        if request.headers.get(TOKEN_HEADER, "") != TOKEN:
            return JSONResponse({"detail": "token invalido o ausente"}, status_code=401)
    return await call_next(request)


# ── Modelos ───────────────────────────────────────────────────────────────────

class ClickReq(BaseModel):
    x: int
    y: int
    button: str = "left"

class MoveReq(BaseModel):
    x: int
    y: int

class DragReq(BaseModel):
    x1: int; y1: int
    x2: int; y2: int
    duration: float = 0.3

class TypeReq(BaseModel):
    text: str
    interval: float = 0.04

class KeyReq(BaseModel):
    key: str

class HotkeyReq(BaseModel):
    keys: list[str]

class ScrollReq(BaseModel):
    x: int
    y: int
    direction: str = "down"
    amount: int = 3

class LaunchReq(BaseModel):
    app: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/screenshot")
def screenshot():
    return take_screenshot()

@app.get("/windows")
def windows():
    return {"windows": get_windows()}

@app.get("/apps")
def apps():
    return {"apps": get_installed_apps()}

@app.get("/elements")
def elements(window: Optional[str] = None):
    return {"elements": get_ui_elements(window)}

@app.post("/click")
def do_click(req: ClickReq):
    click(req.x, req.y, req.button)
    return {"ok": True}

@app.post("/double_click")
def do_double_click(req: ClickReq):
    double_click(req.x, req.y)
    return {"ok": True}

@app.post("/right_click")
def do_right_click(req: ClickReq):
    right_click(req.x, req.y)
    return {"ok": True}

@app.post("/move")
def do_move(req: MoveReq):
    move(req.x, req.y)
    return {"ok": True}

@app.post("/drag")
def do_drag(req: DragReq):
    drag(req.x1, req.y1, req.x2, req.y2, req.duration)
    return {"ok": True}

@app.post("/type")
def do_type(req: TypeReq):
    type_text(req.text, req.interval)
    return {"ok": True}

@app.post("/key")
def do_key(req: KeyReq):
    press_key(req.key)
    return {"ok": True}

@app.post("/hotkey")
def do_hotkey(req: HotkeyReq):
    hotkey(*req.keys)
    return {"ok": True}

@app.post("/scroll")
def do_scroll(req: ScrollReq):
    scroll(req.x, req.y, req.amount, req.direction)
    return {"ok": True}

@app.post("/launch")
def do_launch(req: LaunchReq):
    launch_app(req.app)
    return {"ok": True}


if __name__ == "__main__":
    print(f"[Companion] Iris Desktop Companion corriendo en http://0.0.0.0:{PORT}")
    print(f"[Companion] Autenticacion activa — token en {TOKEN_HEADER}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
