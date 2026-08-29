"""
interfaces/brain_api.py
La API del editor visual del cerebro.

Vive aparte de http_api.py a propósito: aquella es la que usa el portátil para
hablar con el cerebro remoto y va autenticada con el token del enlace. Esta solo
escucha en localhost, la abre el propio dueño desde su máquina, y existe para
mirar y corregir lo que Iris recuerda.

Se sirve sobre el mismo StorageFactory que usa Iris, así que lo que se edita aquí
es lo que ella lee en el turno siguiente — sin reiniciar nada.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from config.settings import settings

logger = logging.getLogger(__name__)

_PAGINA = Path(__file__).parent.parent / "ui" / "brain" / "index.html"

# Quien llega por aquí ya está dentro de la máquina: pedirle una llave para ver
# su propia memoria en su propio ordenador sería teatro.
_LOCALES = {"127.0.0.1", "::1", "localhost"}


class Entidad(BaseModel):
    name: str
    type: str = "Unknown"


class Renombrado(BaseModel):
    old_name: str
    new_name: str


class Relacion(BaseModel):
    from_name: str
    relation: str
    to_name: str
    context: str = ""


class TextoMemoria(BaseModel):
    content: str


def create_brain_api(iris, exigir_token: bool = False) -> FastAPI:
    """
    exigir_token=True cuando esto se sirve desde internet.

    Sin token no se devuelve ni la página: si la sirviéramos y solo protegiéramos
    los datos, cualquiera vería la estructura y sabría exactamente qué pedir.
    """
    app = FastAPI(title="Cerebro de Iris")
    token = settings.brain.token

    def _es_local(req: Request) -> bool:
        return bool(req.client) and req.client.host in _LOCALES

    def _autorizar(req: Request, escribe: bool = False) -> None:
        if not exigir_token or _es_local(req):
            return
        if not token:
            raise HTTPException(503, "Sin BRAIN_TOKEN configurado; el acceso remoto está cerrado.")
        dado = req.query_params.get("k") or req.headers.get("X-Brain-Token", "")
        if dado != token:
            logger.warning(f"[Cerebro] Acceso rechazado desde {req.client.host if req.client else '?'}")
            raise HTTPException(403, "Llave incorrecta.")
        if escribe and not settings.brain.remote_write:
            raise HTTPException(403, "Desde fuera solo se puede mirar. Edita desde el portátil.")

    @app.get("/")
    async def pagina(request: Request):
        _autorizar(request)
        return FileResponse(_PAGINA)

    # ─── Leer ────────────────────────────────────────────────────────────────

    @app.get("/api/grafo")
    async def grafo(request: Request):
        _autorizar(request)
        g = iris.storage.graph
        ents = g.all_entities()
        rels = g.all_relations()
        return {
            "entities": [
                {"id": e["name"], "label": e["name"],
                 "type": e.get("type") or "Unknown", "degree": e.get("grado", 0)}
                for e in ents
            ],
            "relations": [
                {"from": r["from_name"], "relation": r["relation"], "to": r["to_name"],
                 "context": r.get("context") or "", "date": r.get("rel_date") or ""}
                for r in rels
            ],
        }

    @app.get("/api/memorias")
    async def memorias(request: Request):
        _autorizar(request)
        return [
            {"id": m["id"], "content": m.get("content", ""),
             "category": m.get("category", "?"), "importance": m.get("importance", 1),
             "expires_at": str(m["expires_at"]) if m.get("expires_at") else None}
            for m in iris.memory.get_all_memories()
        ]

    @app.get("/api/gustos")
    async def gustos(request: Request):
        _autorizar(request)
        return [
            {"subject": p.subject, "kind": p.kind, "valence": round(p.valence, 2),
             "strength": round(p.current_strength, 2), "intensity": p.intensity}
            for p in iris.preferences.all()
        ]

    @app.get("/api/estado")
    async def estado(request: Request):
        _autorizar(request)
        s = iris.personality.state
        return {"mood": s.mood.value, "trust": round(s.trust_level, 1),
                "energy": round(s.energy), "stage": iris.personality.get_trust_stage().value}

    # ─── Escribir ────────────────────────────────────────────────────────────

    def _refrescar():
        """Que el matcher relea los nombres: si no, sigue con los de antes 10 min."""
        try:
            iris.memory._entity_matcher.invalidate()
        except Exception:
            pass

    @app.post("/api/entidad")
    async def crear_entidad(e: Entidad, request: Request):
        _autorizar(request, escribe=True)
        iris.storage.graph.add_entity(e.name, e.type, {})
        _refrescar()
        return {"ok": True}

    @app.post("/api/entidad/renombrar")
    async def renombrar(r: Renombrado, request: Request):
        _autorizar(request, escribe=True)
        if not iris.storage.graph.rename_entity(r.old_name, r.new_name):
            raise HTTPException(404, f"No existe «{r.old_name}»")
        _refrescar()
        return {"ok": True}

    @app.delete("/api/entidad/{name}")
    async def borrar_entidad(name: str, request: Request):
        _autorizar(request, escribe=True)
        n = iris.storage.graph.delete_entity(name)
        if n < 0:
            raise HTTPException(404, f"No existe «{name}»")
        _refrescar()
        return {"ok": True, "relaciones_borradas": n}

    @app.post("/api/relacion")
    async def crear_relacion(r: Relacion, request: Request):
        _autorizar(request, escribe=True)
        g = iris.storage.graph
        # Las dos puntas tienen que existir: la FK lo exige, y crear el nodo que
        # falte es lo que uno espera al dibujar una flecha hacia algo nuevo.
        existentes = {e["name"] for e in g.all_entities()}
        for punta in (r.from_name, r.to_name):
            if punta not in existentes:
                g.add_entity(punta, "Unknown", {})
        g.add_relation(r.from_name, r.relation, r.to_name, {"context": r.context})
        _refrescar()
        return {"ok": True}

    @app.delete("/api/relacion")
    async def borrar_relacion(r: Relacion, request: Request):
        _autorizar(request, escribe=True)
        if not iris.storage.graph.delete_relation(r.from_name, r.relation, r.to_name):
            raise HTTPException(404, "Esa relación no existe")
        return {"ok": True}

    @app.patch("/api/memoria/{memory_id}")
    async def editar_memoria(memory_id: str, t: TextoMemoria, request: Request):
        _autorizar(request, escribe=True)
        if not iris.storage.vector.update(memory_id, t.content):
            raise HTTPException(404, "No existe esa memoria")
        return {"ok": True}

    @app.delete("/api/memoria/{memory_id}")
    async def borrar_memoria(memory_id: str, request: Request):
        _autorizar(request, escribe=True)
        if not iris.storage.vector.delete(memory_id):
            raise HTTPException(404, "No existe esa memoria")
        return {"ok": True}

    return app


def serve(iris, port: int = 8765) -> str:
    """
    Levanta el editor en un hilo y devuelve su URL.

    En localhost y nada más: esto expone la memoria entera sin autenticación, y
    lo hace a propósito —es tu máquina y tu cabeza— pero por eso no puede
    escuchar en 0.0.0.0.
    """
    import threading
    import uvicorn

    app = create_brain_api(iris)

    def _run():
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

    threading.Thread(target=_run, daemon=True, name="iris-brain-ui").start()
    return f"http://127.0.0.1:{port}"
