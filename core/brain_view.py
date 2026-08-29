"""
core/brain_view.py
Ver y editar lo que Iris tiene en la cabeza.

Existe porque hasta ahora su memoria solo se podía mirar de refilón —`/memoria`
enseñaba tres entradas sin identificador— y no se podía corregir nada. Y sí hace
falta corregir: la extracción confunde "Lucia" con "Lucía", guarda como hecho
duradero algo que dijiste de pasada, o cruza dos entidades que no tienen que ver.

Todo lo que borra pide confirmación repitiendo el comando con `ya`. No es
ceremonia: son datos que no están en ningún otro sitio y no hay deshacer.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_AYUDA = """Qué tiene Iris en la cabeza, y cómo tocarlo.

  /cerebro web                    abre el grafo interactivo en el navegador
  /cerebro                        resumen de todo
  /cerebro grafo                  entidades y relaciones
  /cerebro quien <nombre>         todo lo que sabe de alguien
  /cerebro memorias [texto]       memorias con su id (filtra si pones texto)
  /cerebro gustos                 lo que le gusta y lo que no

Para corregir:
  /cerebro olvida <id>            borra una memoria
  /cerebro corrige <id> <texto>   reescribe una memoria
  /cerebro renombra <viejo> <nuevo>   une dos entidades ("Lucia" → "Lucía")
  /cerebro desconecta <a> <REL> <b>   borra una relación
  /cerebro borra-nodo <nombre>    borra una entidad y sus relaciones

Lo que borra pide confirmación: repite el comando añadiendo `ya` al final."""


def _corta(texto: str, n: int) -> str:
    texto = " ".join((texto or "").split())
    return texto if len(texto) <= n else texto[: n - 1] + "…"


def handle(iris, args: list[str]) -> str:
    """Despacha /cerebro. `args` son las palabras después del comando."""
    if not args:
        return _resumen(iris)

    sub, resto = args[0].lower(), args[1:]
    confirmado = bool(resto) and resto[-1].lower() == "ya"
    if confirmado:
        resto = resto[:-1]

    match sub:
        case "ayuda" | "help":      return _AYUDA
        case "web" | "ver":         return _abrir_web(iris)
        case "grafo":               return _grafo(iris)
        case "quien" | "quién":     return _quien(iris, " ".join(resto))
        case "memorias":            return _memorias(iris, " ".join(resto))
        case "gustos":              return iris.preferences.summary()
        case "olvida":              return _olvida(iris, resto, confirmado)
        case "corrige":             return _corrige(iris, resto)
        case "renombra":            return _renombra(iris, resto)
        case "desconecta":          return _desconecta(iris, resto, confirmado)
        case "borra-nodo":          return _borra_nodo(iris, resto, confirmado)
        case _:
            return f"No sé qué es «{sub}».\n\n{_AYUDA}"


_web_url: Optional[str] = None


def _abrir_web(iris) -> str:
    """
    Da acceso al editor visual, y cómo depende de dónde corra esto.

    En el servidor no hay navegador que abrir ni sirve un 127.0.0.1: lo que hace
    falta es el enlace público con la llave dentro, para abrirlo desde donde
    estés. En local se abre el navegador y ya, sin token, porque quien está en la
    máquina ya está dentro.
    """
    from config.settings import settings

    if settings.mode.mode == "server":
        base = settings.server.public_url
        if not base:
            return ("El editor está montado, pero no sé mi propia URL pública "
                    "(falta IRIS_DOMAIN). Sin eso no puedo darte el enlace.")
        if not settings.brain.token:
            return "El editor no está abierto: falta BRAIN_TOKEN en el servidor."
        solo_lectura = "" if settings.brain.remote_write else "\nDesde fuera solo se puede mirar; para editar, el portátil."
        return (f"{base}/cerebro/?k={settings.brain.token}\n"
                f"Ese enlace lleva la llave dentro: quien lo tenga entra.{solo_lectura}")

    global _web_url
    import webbrowser
    try:
        if _web_url is None:
            from interfaces.brain_api import serve
            _web_url = serve(iris)
            logger.info(f"[Cerebro] Editor visual en {_web_url}")
        webbrowser.open(_web_url)
        return (f"Abriendo el grafo en {_web_url}\n"
                "Clic en un nodo para editarlo; arrastra para moverlo. "
                "Lo que cambies ahí lo lee Iris en el turno siguiente.")
    except Exception as e:
        logger.warning(f"[Cerebro] No pude abrir el editor: {e}")
        return f"No pude abrir el editor visual: {e}"


# ─── Mirar ────────────────────────────────────────────────────────────────────

def _resumen(iris) -> str:
    g = iris.storage.graph
    try:
        ents, rels = g.all_entities(), g.all_relations()
    except Exception as e:
        ents, rels = [], []
        logger.warning(f"[Cerebro] No pude leer el grafo: {e}")

    mem = iris.memory.get_all_memories()
    por_cat: dict = {}
    for m in mem:
        por_cat[m.get("category", "?")] = por_cat.get(m.get("category", "?"), 0) + 1

    filas = [
        f"{len(mem)} memorias" + (
            "  (" + ", ".join(f"{c}:{n}" for c, n in sorted(por_cat.items(), key=lambda x: -x[1])) + ")"
            if por_cat else ""
        ),
        f"{len(ents)} entidades y {len(rels)} relaciones en el grafo",
        f"{iris.preferences.count()} gustos formados",
    ]
    try:
        filas.append(f"{iris.storage.journal.count()} entradas de diario")
    except Exception:
        pass
    filas.append(f"{iris.personality.get_status_summary()}")

    if ents:
        top = ", ".join(f"{e['name']} ({e['grado']})" for e in ents[:5])
        filas += ["", f"Lo más conectado: {top}"]
    elif mem:
        filas += ["", "El grafo está vacío habiendo memorias — algo va mal en la extracción."]

    filas += ["", "`/cerebro web` para verlo como grafo · `/cerebro ayuda` para el resto."]
    return "\n".join(filas)


def _grafo(iris) -> str:
    g = iris.storage.graph
    ents, rels = g.all_entities(), g.all_relations()
    if not ents:
        return "El grafo está vacío."

    salida = [f"{len(ents)} entidades, {len(rels)} relaciones", ""]
    por_tipo: dict = {}
    for e in ents:
        por_tipo.setdefault(e.get("type") or "?", []).append(e)
    for tipo, lista in sorted(por_tipo.items()):
        nombres = ", ".join(f"{e['name']}·{e['grado']}" for e in lista)
        salida.append(f"[{tipo}] {nombres}")

    salida += ["", "Relaciones:"]
    for r in rels:
        ctx = f"  — {_corta(r.get('context') or '', 50)}" if r.get("context") else ""
        salida.append(f"  {r['from_name']} —{r['relation']}→ {r['to_name']}{ctx}")
    return "\n".join(salida)


def _quien(iris, nombre: str) -> str:
    if not nombre:
        return "¿De quién? Ej: /cerebro quien Lucía"
    g = iris.storage.graph
    rels = [r for r in g.all_relations()
            if nombre.lower() in (r["from_name"].lower(), r["to_name"].lower())]
    mems = [m for m in iris.memory.get_all_memories()
            if nombre.lower() in (m.get("content") or "").lower()]
    if not rels and not mems:
        return f"No sabe nada de «{nombre}»."

    salida = [f"— {nombre} —"]
    if rels:
        salida.append("\nEn el grafo:")
        for r in rels:
            salida.append(f"  {r['from_name']} —{r['relation']}→ {r['to_name']}")
    if mems:
        salida.append("\nEn sus memorias:")
        for m in mems[:10]:
            salida.append(f"  [{m.get('category','?')}] {_corta(m.get('content',''), 90)}")
    return "\n".join(salida)


def _memorias(iris, filtro: str) -> str:
    mem = iris.memory.get_all_memories()
    if filtro:
        mem = [m for m in mem if filtro.lower() in (m.get("content") or "").lower()]
    if not mem:
        return "Nada que enseñar." if not filtro else f"Ninguna memoria menciona «{filtro}»."

    salida = [f"{len(mem)} memorias:" if not filtro else f"{len(mem)} con «{filtro}»:", ""]
    for m in mem:
        caduca = f" · caduca {m['expires_at']}" if m.get("expires_at") else ""
        salida.append(f"{m['id'][:8]}  [{m.get('category','?')}]{caduca}")
        salida.append(f"          {_corta(m.get('content',''), 95)}")
    salida += ["", "El id corto vale para /cerebro olvida y /cerebro corrige."]
    return "\n".join(salida)


# ─── Tocar ────────────────────────────────────────────────────────────────────

def _buscar_memoria(iris, id_corto: str) -> Optional[dict]:
    """Acepta el id abreviado que muestra el listado, si no es ambiguo."""
    coincide = [m for m in iris.memory.get_all_memories()
                if m["id"].startswith(id_corto)]
    return coincide[0] if len(coincide) == 1 else None


def _olvida(iris, resto: list[str], confirmado: bool) -> str:
    if not resto:
        return "¿Cuál? Mira los ids con /cerebro memorias"
    m = _buscar_memoria(iris, resto[0])
    if not m:
        return f"No encuentro una memoria que empiece por «{resto[0]}» (o hay varias)."
    if not confirmado:
        return (f"Se borraría:\n  {_corta(m.get('content',''), 120)}\n\n"
                f"No hay deshacer. Confirma con:  /cerebro olvida {resto[0]} ya")
    ok = iris.storage.vector.delete(m["id"])
    return "Olvidado." if ok else "No pude borrarla."


def _corrige(iris, resto: list[str]) -> str:
    if len(resto) < 2:
        return "Uso: /cerebro corrige <id> <el texto correcto>"
    m = _buscar_memoria(iris, resto[0])
    if not m:
        return f"No encuentro una memoria que empiece por «{resto[0]}» (o hay varias)."
    nuevo = " ".join(resto[1:])
    ok = iris.storage.vector.update(m["id"], nuevo)
    if not ok:
        return "No pude corregirla."
    return (f"Antes: {_corta(m.get('content',''), 90)}\n"
            f"Ahora: {_corta(nuevo, 90)}")


def _renombra(iris, resto: list[str]) -> str:
    if len(resto) < 2:
        return "Uso: /cerebro renombra <nombre viejo> <nombre nuevo>"
    viejo, nuevo = resto[0], " ".join(resto[1:])
    ok = iris.storage.graph.rename_entity(viejo, nuevo)
    if not ok:
        return f"No existe ninguna entidad «{viejo}»."
    iris.memory._entity_matcher.invalidate()   # que relea los nombres
    return f"«{viejo}» ahora es «{nuevo}», con sus relaciones."


def _desconecta(iris, resto: list[str], confirmado: bool) -> str:
    if len(resto) < 3:
        return "Uso: /cerebro desconecta <origen> <RELACION> <destino>"
    a, rel, b = resto[0], resto[1], " ".join(resto[2:])
    if not confirmado:
        return f"Se borraría:  {a} —{rel}→ {b}\nConfirma con:  /cerebro desconecta {a} {rel} {b} ya"
    ok = iris.storage.graph.delete_relation(a, rel, b)
    return "Desconectados." if ok else "Esa relación no existe."


def _borra_nodo(iris, resto: list[str], confirmado: bool) -> str:
    if not resto:
        return "Uso: /cerebro borra-nodo <nombre>"
    nombre = " ".join(resto)
    if not confirmado:
        rels = [r for r in iris.storage.graph.all_relations()
                if nombre in (r["from_name"], r["to_name"])]
        return (f"Se borraría «{nombre}» y sus {len(rels)} relaciones.\n"
                f"Confirma con:  /cerebro borra-nodo {nombre} ya")
    n = iris.storage.graph.delete_entity(nombre)
    if n < 0:
        return f"No existe ninguna entidad «{nombre}»."
    iris.memory._entity_matcher.invalidate()
    return f"Borrado «{nombre}» y {n} relaciones."
