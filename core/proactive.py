"""
core/proactive.py
Motor de iniciativa propia de Iris.

Corre un hilo de fondo que evalúa cada CHECK_INTERVAL si Iris quiere
iniciar una conversación. Condiciones de disparo:
  - Lleva >= 4h sin interacción Y mood == LONELY
  - Lleva >= 4h sin interacción Y energía recuperada a 100

Restricciones:
  - Horario de silencio: 00:00–08:00
  - Máximo MAX_PER_DAY mensajes proactivos por día
  - El LLM decide si tiene algo genuino que decir ([SILENCIO] = no enviar)
"""

import logging
import threading
from datetime import datetime
from typing import Callable

from langchain_core.messages import HumanMessage, SystemMessage

from config.prompts import PROACTIVE_PROMPT
from core.personality import Mood

logger = logging.getLogger(__name__)

_QUIET_START    = 0     # hora (00:xx)
_QUIET_END      = 8     # hora (08:xx)
_MAX_PER_DAY    = 2
_MIN_HOURS_IDLE = 4.0
_CHECK_INTERVAL = 30 * 60   # 30 minutos
_INITIAL_DELAY  = 90        # segundos tras el arranque antes del primer check


class ProactiveEngine:
    """
    Evalúa periódicamente si Iris quiere iniciar una conversación.

    send_fn: callable(text: str) — entrega el mensaje al canal correcto
             (Telegram y/o UI de escritorio). Lo maneja main.py.
    interface_context: prompt adicional de canal (ej. TELEGRAM_INTERFACE_ADDON)
                       que se antepone al system prompt cuando se genera el mensaje.
    """

    def __init__(
        self,
        iris,
        send_fn: Callable[[str], None],
        interface_context: str = "",
    ):
        self.iris              = iris
        self.send_fn           = send_fn
        self.interface_context = interface_context
        self._stop             = threading.Event()

    # ─── Ciclo de vida ────────────────────────────────────────────────────────

    def start(self):
        t = threading.Thread(target=self._loop, daemon=True, name="iris-proactive")
        t.start()
        logger.info("[Proactive] Motor de iniciativa activo.")

    def stop(self):
        self._stop.set()

    # ─── Loop interno ─────────────────────────────────────────────────────────

    def _loop(self):
        self._stop.wait(_INITIAL_DELAY)
        while not self._stop.is_set():
            try:
                self._evaluate()
            except Exception as e:
                logger.warning(f"[Proactive] Error en evaluación: {e}")
            self._stop.wait(_CHECK_INTERVAL)

    def _evaluate(self):
        now  = datetime.now()
        pers = self.iris.personality

        # Horario de silencio
        if _QUIET_START <= now.hour < _QUIET_END:
            return

        # Límite diario
        if not pers.can_send_proactive(_MAX_PER_DAY):
            return

        # Condiciones de disparo
        hours = pers.hours_since_last_interaction()
        if hours < _MIN_HOURS_IDLE:
            return

        mood    = pers.state.mood
        energy  = pers.state.energy
        lonely  = mood == Mood.LONELY
        rested  = energy >= 99.0

        if not (lonely or rested):
            return

        logger.info(
            f"[Proactive] Condición cumplida — {hours:.1f}h idle, "
            f"mood={mood.value}, energy={energy:.0f}"
        )

        msg = self._generate(hours)
        if not msg or "[SILENCIO]" in msg:
            logger.info("[Proactive] Iris eligió no escribir.")
            return

        logger.info(f"[Proactive] Iris inicia: {msg[:80]}")
        self.send_fn(msg)
        pers.record_proactive_sent()
        # Registrar en historial como turno iniciado por Iris
        self.iris.history.append_turn("[Iris inició la conversación]", msg)
        pers.state.mood = Mood.NEUTRAL
        pers.save_state()

    # ─── Generación del mensaje ───────────────────────────────────────────────

    def _generate(self, hours_since: float) -> str:
        pers       = self.iris.personality
        memory_ctx = self.iris.memory.get_relevant_memories("conversación reciente interacción")
        hint       = f"\nRecuerdos recientes: {memory_ctx}" if memory_ctx else ""

        prompt = PROACTIVE_PROMPT.format(
            hours_since = hours_since,
            owner_name  = pers.owner_name or "él",
            mood        = pers.state.mood.value,
            energy      = pers.state.energy,
            memory_hint = hint,
        )

        system = pers.build_system_prompt()
        if self.interface_context:
            system = self.interface_context + "\n\n" + system

        msgs = [SystemMessage(content=system), HumanMessage(content=prompt)]
        try:
            resp = self.iris.llm.invoke(msgs)
            return resp.content.strip()
        except Exception as e:
            logger.warning(f"[Proactive] Error LLM: {e}")
            return ""
