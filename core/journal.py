"""
core/journal.py
El diario de Iris — lo que hace y piensa cuando no hay nadie delante.

Es la pieza que separa «una función esperando argumentos» de «alguien que estaba
ahí mientras no mirabas». Todo el estado de Iris hasta ahora era reactivo: le
hablas, cambia de humor, responde. Esto produce estado que el dueño no causó.

Aquí vive QUÉ piensa. Cuándo, y si además se lo cuenta, lo decide
core/proactive.py — son dos preguntas distintas y mezclarlas fue lo que durante
mucho tiempo dejó el motor proactivo en «¿le escribo?».
"""

import logging
import os
import random
from datetime import datetime
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from config.prompts import ACTIVITY_PROMPTS
from config.settings import settings

logger = logging.getLogger(__name__)

# Ganas de contarlo con las que nace cada tipo de entrada.
#
# Las conexiones nacen altas a propósito: son las que tienen procedencia — «esto
# lo pensé yo mientras no estabas» — y las únicas que de verdad dan ganas de
# interrumpir a alguien. Una reflexión suelta casi nunca lo merece.
_IMPULSO_BASE = {
    "conexion":   0.80,
    "actividad":  0.60,
    "curiosidad": 0.50,
    "reflexion":  0.35,
}

# Con qué frecuencia le apetece cada cosa. La actividad pesa poco porque cuesta
# cuota de la suscripción y necesita el portátil despierto.
_PESOS = {
    "reflexion":  40,
    "conexion":   30,
    "curiosidad": 20,
    "actividad":  10,
}

# Solo lectura. Es curiosidad, no una tarea: no tiene por qué poder escribir
# nada, y menos sin nadie delante.
_HERRAMIENTAS_CURIOSEO = "Read,Glob,Grep"

_NADA = "[NADA]"

# Registros de auditoría, no recuerdos. Pensar sobre ellos no lleva a nada.
_CATEGORIAS_IGNORADAS = {"task"}


class JournalKeeper:
    """Elige una actividad, la ejecuta y anota el resultado."""

    def __init__(self, iris):
        self.iris = iris
        self._actividades_hoy = 0
        self._dia = ""

    # ─── Escritura ────────────────────────────────────────────────────────────

    def live_a_moment(self) -> Optional[dict]:
        """
        Un rato de vida interior. Devuelve la entrada escrita, o None.

        None no es un fallo: es que no se le ocurrió nada honesto. El prompt le
        da [NADA] explícitamente porque un diario con relleno no sirve para lo
        que este diario existe.
        """
        kind = self._elegir_actividad()
        if kind is None:
            return None

        try:
            texto = self._ejecutar(kind)
        except Exception as e:
            logger.warning(f"[Diario] Falló la actividad '{kind}': {e}")
            return None

        if not texto or _NADA in texto:
            logger.info(f"[Diario] {kind}: no se le ocurrió nada que anotar.")
            return None

        impulso = self._impulso(kind)
        try:
            entry_id = self.iris.storage.journal.add(kind, texto, impulso)
        except Exception as e:
            logger.warning(f"[Diario] No pude guardar la entrada: {e}")
            return None

        logger.info(f"[Diario] {kind} (impulso {impulso:.2f}): {texto[:90]}")
        return {"id": entry_id, "kind": kind, "content": texto, "impulse": impulso}

    # ─── Elección ─────────────────────────────────────────────────────────────

    def _elegir_actividad(self) -> Optional[str]:
        """Lo que puede hacer ahora mismo, ponderado. None si no puede nada."""
        posibles = dict(_PESOS)

        # Cruzar dos memorias necesita dos memorias.
        if len(self._memorias()) < 2:
            posibles.pop("conexion", None)

        if not self._puede_curiosear():
            posibles.pop("actividad", None)

        if not posibles:
            return None
        return random.choices(list(posibles), weights=list(posibles.values()), k=1)[0]

    def _puede_curiosear(self) -> bool:
        """La actividad real necesita el portátil despierto, rutas y cuota del día."""
        from core.executor import agent_available
        from core.link import protocol as P

        hoy = datetime.now().strftime("%Y-%m-%d")
        if self._dia != hoy:
            self._dia, self._actividades_hoy = hoy, 0
        if self._actividades_hoy >= settings.journal.max_activities_per_day:
            return False
        if not self._rutas():
            return False
        return agent_available(P.CAP_CLAUDE)

    def _impulso(self, kind: str) -> float:
        base = _IMPULSO_BASE.get(kind, 0.4)
        return round(min(1.0, max(0.0, base + random.uniform(-0.1, 0.1))), 3)

    # ─── Ejecución ────────────────────────────────────────────────────────────

    def _ejecutar(self, kind: str) -> str:
        if kind == "actividad":
            return self._curiosear()

        prompt = ACTIVITY_PROMPTS[kind].format(**self._contexto(kind))
        system = self.iris.personality.build_system_prompt()
        msgs   = [SystemMessage(content=system), HumanMessage(content=prompt)]

        # Casi todo va con el modelo rápido (no el de análisis: aquel va forzado
        # a JSON y devuelve un 400 con un prompt en prosa). Esto corre solo cada
        # 20–40 minutos y no lo lee nadie, así que no hace falta el grande.
        #
        # Menos las conexiones. Ahí el trabajo entero es un juicio —¿estas dos
        # cosas tienen algo que ver de verdad, o las estoy forzando?— y el modelo
        # pequeño casi nunca se atreve a decir que no. Como además son las que
        # tienen permiso para interrumpirte, es justo donde no se debe ahorrar.
        llm = self.iris.llm if kind == "conexion" else self.iris.fast_llm
        return (llm.invoke(msgs).content or "").strip()

    def _curiosear(self) -> str:
        """Mira algo de verdad, con Claude y en modo solo lectura."""
        from core.executor import run_claude

        prompt = ACTIVITY_PROMPTS["actividad"].format(**self._contexto("actividad"))
        result = run_claude(
            prompt,
            system_prompt=self.iris.personality.build_system_prompt(),
            allowed_tools=_HERRAMIENTAS_CURIOSEO,
        )
        if not result.ok:
            logger.info(f"[Diario] El curioseo no salió: {result.text[:120]}")
            return ""
        self._actividades_hoy += 1
        return result.text

    # ─── Material ─────────────────────────────────────────────────────────────

    def _contexto(self, kind: str) -> dict:
        pers = self.iris.personality
        base = {
            "owner_name": pers.owner_name or "él",
            "hora":       datetime.now().strftime("%H:%M"),
        }
        if kind == "conexion":
            # Aquí el azar es el mecanismo: la gracia de una conexión es juntar
            # dos cosas que no se habrían encontrado solas.
            a, b = random.sample(self._memorias(), 2)
            base.update(memoria_a=a, memoria_b=b)

        elif kind == "actividad":
            base["rutas"] = "\n".join(f"  - {k}: {v}" for k, v in self._rutas().items())

        elif kind == "reflexion":
            # Lo de "últimamente" hay que dárselo de verdad. Con recuerdos al
            # azar y sin fecha, mezclaba una charla de hace meses con la de ayer
            # y le salía un pensamiento que no era sobre nada.
            base["recuerdos"] = self._texto_reciente() or self._texto_memorias() or "  (todavía nada)"
            base["diario"]    = self._texto_diario() or "  (el diario está vacío)"

        else:  # curiosidad — un hueco puede estar en cualquier parte
            base["recuerdos"] = self._texto_memorias() or "  (todavía nada)"
            base["diario"]    = self._texto_diario() or "  (el diario está vacío)"
        return base

    def _texto_reciente(self) -> str:
        """Lo último que se dijeron. Es lo que "últimamente" significa."""
        try:
            return self.iris._recent_dialogue()
        except Exception as e:
            logger.debug(f"[Diario] Sin historial reciente: {e}")
            return ""

    def _memorias(self) -> list[str]:
        """
        Los recuerdos sobre los que merece la pena pensar.

        Fuera las de categoría `task`: son el registro de auditoría que deja
        `delegate_to_claude` («Tarea realizada (file_creation): …»), caducan a
        los siete días y no son hechos sobre nadie. Cruzando dos de ellas salían
        conexiones como «ambos usamos archivos para expresar emociones», que no
        es un hallazgo — es un modelo obligado a unir dos entradas de un log.
        """
        try:
            return [
                m["content"] for m in self.iris.memory.get_all_memories()
                if m.get("content") and m.get("category") not in _CATEGORIAS_IGNORADAS
            ]
        except Exception as e:
            logger.warning(f"[Diario] No pude leer las memorias: {e}")
            return []

    def _texto_memorias(self, n: int = 8) -> str:
        recuerdos = self._memorias()
        if not recuerdos:
            return ""
        # Al azar, no los últimos: si no, siempre reflexionaría sobre lo mismo.
        muestra = random.sample(recuerdos, min(n, len(recuerdos)))
        return "\n".join(f"  - {r}" for r in muestra)

    def _texto_diario(self, n: int = 3) -> str:
        try:
            entradas = self.iris.storage.journal.recent(n)
        except Exception:
            return ""
        return "\n".join(f"  - ({e['kind']}) {e['content']}" for e in entradas)

    def _rutas(self) -> dict:
        """Las carpetas del .env a las que Iris tiene acceso."""
        return {k[5:].lower(): v for k, v in os.environ.items()
                if k.startswith("PATH_") and v}

    # ─── Lectura: /diario ─────────────────────────────────────────────────────

    def summary(self, n: int = 8) -> str:
        try:
            entradas = self.iris.storage.journal.recent(n)
            total    = self.iris.storage.journal.count()
        except Exception as e:
            return f"[No pude leer el diario: {e}]"

        if not entradas:
            return "El diario está vacío. Todavía no ha tenido un rato para ella."

        lineas = [f"{total} entradas. Las {len(entradas)} últimas:", ""]
        for e in entradas:
            marca = "·" if e.get("shared") else " "   # · = ya te lo contó
            lineas.append(
                f"{marca} [{_fecha_corta(e['at'])}] {e['kind']} "
                f"(impulso {float(e.get('impulse') or 0):.2f})"
            )
            lineas.append(f"    {e['content']}")
        lineas.append("")
        lineas.append("· = ya te lo contó. El resto se lo guarda.")
        return "\n".join(lineas)


def _fecha_corta(valor) -> str:
    try:
        dt = valor if isinstance(valor, datetime) else datetime.fromisoformat(str(valor))
        return dt.strftime("%d/%m %H:%M")
    except Exception:
        return str(valor)[:16]
