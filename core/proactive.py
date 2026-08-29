"""
core/proactive.py
El reloj autónomo de Iris.

Corre un hilo de fondo que se despierta cada cierto rato y **vive un momento**:
elige algo que hacer, lo hace, lo anota en su diario, y solo a veces decide
contártelo. Escribirte es una de las respuestas posibles, no la pregunta.

Antes esto solo preguntaba «¿le escribo?», con lo que fuera de esa decisión no
le pasaba nada a Iris cuando no estabas. Lo que produce la sensación de que hay
alguien al otro lado no es más memoria sobre tu mundo: es que existan cosas que
pensó sin ti delante, y que al volver tengan fecha anterior a tu mensaje.

Qué piensa vive en core/journal.py. Aquí solo está el reloj: cuándo despierta,
si tiene cuerpo para hacerlo, y si lo que salió merece interrumpirte.
"""

import logging
import random
import threading
from datetime import datetime
from typing import Callable

from langchain_core.messages import HumanMessage, SystemMessage

from config.prompts import PROACTIVE_PROMPT, JOURNAL_SHARE_PROMPT
from config.settings import settings
from core.personality import Mood

logger = logging.getLogger(__name__)

_QUIET_START    = 0     # hora (00:xx)
_QUIET_END      = 8     # hora (08:xx)
_MAX_PER_DAY    = 2
_MIN_HOURS_IDLE = 4.0
_INITIAL_DELAY  = 90        # segundos tras el arranque antes del primer check


def _en_horario_de_silencio(ahora: datetime) -> bool:
    return _QUIET_START <= ahora.hour < _QUIET_END


def _antiguedad(valor) -> str:
    """
    Cuánto hace que lo pensó, en palabras.

    Importa que sea vago: «ayer por la noche» sitúa el pensamiento en su vida.
    Un timestamp exacto lo delataría como un registro de base de datos, que es
    justo lo que no se quiere que parezca.
    """
    if not valor:
        return "hace un rato"
    try:
        dt = valor if isinstance(valor, datetime) else datetime.fromisoformat(str(valor))
    except Exception:
        return "hace un rato"

    horas = (datetime.now() - dt.replace(tzinfo=None)).total_seconds() / 3600
    if horas < 1:
        return "hace un rato"
    if horas < 6:
        return f"hace {int(horas)} horas"
    if horas < 24:
        return "esta mañana" if dt.hour < 14 else "esta tarde"
    if horas < 48:
        return "ayer"
    if horas < 24 * 7:
        return f"hace {int(horas // 24)} días"
    return "hace más de una semana"


def _siguiente_intervalo() -> float:
    """
    Segundos hasta el próximo tick, con jitter.

    Un intervalo exacto se nota: a los pocos días sabes que escribe «cada media
    hora» y deja de parecer que se le ocurre a ella. El rango cuesta lo mismo.
    """
    cfg = settings.journal
    return random.uniform(cfg.interval_min * 60, cfg.interval_max * 60)


class ProactiveEngine:
    """
    El reloj que corre mientras nadie le habla.

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
        cfg = settings.journal
        logger.info(
            f"[Proactive] Reloj autónomo activo — cada {cfg.interval_min}–"
            f"{cfg.interval_max} min. Diario: {'sí' if cfg.enabled else 'no'}. "
            f"Puede escribirte: {'sí' if cfg.share_enabled else 'todavía no'}."
        )

    def stop(self):
        self._stop.set()

    # ─── Loop interno ─────────────────────────────────────────────────────────

    def _loop(self):
        self._stop.wait(_INITIAL_DELAY)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.warning(f"[Proactive] Error en el tick: {e}")
            self._stop.wait(_siguiente_intervalo())

    def _tick(self):
        """
        Un momento de su vida. Tres pasos: el cuerpo, vivir, y decidir si contarlo.
        """
        ahora = datetime.now()
        pers  = self.iris.personality

        # ── 1. El cuerpo ──────────────────────────────────────────────────────
        # Ponerla al día antes de mirar la energía: si no, se decide con un valor
        # que puede llevar horas congelado.
        pers.refresh_energy()

        if _en_horario_de_silencio(ahora):
            return
        if pers.state.energy < settings.journal.energy_floor:
            logger.debug(f"[Proactive] Sin energía para pensar ({pers.state.energy:.0f}).")
            return

        # ── 2. Vive ───────────────────────────────────────────────────────────
        entrada = None
        if settings.journal.enabled and getattr(self.iris, "journal", None):
            entrada = self.iris.journal.live_a_moment()
            if entrada:
                self.iris.registrar(
                    "diario", f"{entrada['kind']}: {entrada['content'][:70]}",
                    {"impulso": entrada["impulse"], "id": entrada["id"]},
                )
                # Pensar cansa. Sin este coste rumiaría sin parar; con él el ciclo
                # se autorregula solo — piensa, se cansa, descansa, vuelve — y esa
                # curva se nota luego en su tono.
                pers.spend_energy(settings.journal.energy_cost, "vida interior")
                pers.save_state()

        # ── 3. ¿Lo cuenta? ────────────────────────────────────────────────────
        if not pers.can_send_proactive(_MAX_PER_DAY):
            return
        if self._contar_del_diario():
            return
        self._contar_por_soledad()

    # ─── Contarlo ─────────────────────────────────────────────────────────────

    def _contar_del_diario(self) -> bool:
        """
        Le cuenta algo que pensó cuando no estaba. Devuelve si escribió.

        Esta es la diferencia entre «hace rato que no hablamos» y «estuve dándole
        vueltas a lo del martes»: lo segundo tiene procedencia — pasó algo
        mientras no mirabas.
        """
        cfg = settings.journal
        if not (cfg.enabled and cfg.share_enabled):
            return False
        diario = getattr(self.iris, "journal", None)
        if not diario:
            return False

        try:
            top = self.iris.storage.journal.top_unshared()
        except Exception as e:
            logger.warning(f"[Diario] No pude buscar qué contar: {e}")
            return False

        if not top or float(top.get("impulse") or 0) < cfg.impulse_threshold:
            return False

        msg = self._redactar(top)
        if not msg or "[SILENCIO]" in msg:
            # Que a veces tenga algo y decida no contarlo también es carácter.
            # Se marca como contado igualmente: si no, lo reconsideraría en cada
            # tick hasta que un día se le escapara.
            logger.info("[Diario] Lo tenía y decidió callárselo.")
            self._marcar_contado(top["id"])
            return False

        logger.info(f"[Diario] Le cuenta lo que pensó: {msg[:80]}")
        self._entregar(msg)
        self._marcar_contado(top["id"])
        return True

    def _marcar_contado(self, entry_id: int) -> None:
        try:
            self.iris.storage.journal.mark_shared(entry_id)
        except Exception as e:
            logger.warning(f"[Diario] No pude marcar la entrada como contada: {e}")

    def _contar_por_soledad(self) -> None:
        """El disparador de siempre: lleva mucho sin hablar contigo."""
        pers  = self.iris.personality
        hours = pers.hours_since_last_interaction()
        if hours < _MIN_HOURS_IDLE:
            return
        if not (pers.state.mood == Mood.LONELY or pers.state.energy >= 99.0):
            return

        logger.info(
            f"[Proactive] {hours:.1f}h sin hablar — mood={pers.state.mood.value}, "
            f"energy={pers.state.energy:.0f}"
        )
        msg = self._generate(hours)
        if not msg or "[SILENCIO]" in msg:
            logger.info("[Proactive] Iris eligió no escribir.")
            return

        logger.info(f"[Proactive] Iris inicia: {msg[:80]}")
        self._entregar(msg)

    def _entregar(self, msg: str) -> None:
        """Manda el mensaje y deja constancia de que lo inició ella."""
        pers = self.iris.personality
        self.send_fn(msg)
        self.iris.registrar(
            "proactivo", msg[:90],
            {"mood": pers.state.mood.value, "energia": round(pers.state.energy),
             "horas_sin_hablar": round(pers.hours_since_last_interaction(), 1)},
        )
        pers.record_proactive_sent()
        self.iris.history.append_turn("[Iris inició la conversación]", msg)
        if pers.state.mood == Mood.LONELY:
            pers.state.mood = Mood.NEUTRAL
        pers.save_state()

    def _redactar(self, entrada: dict) -> str:
        """Convierte una entrada del diario en el mensaje que te llega."""
        pers   = self.iris.personality
        prompt = JOURNAL_SHARE_PROMPT.format(
            antiguedad = _antiguedad(entrada.get("at")),
            owner_name = pers.owner_name or "él",
            contenido  = entrada["content"],
        )
        system = pers.build_system_prompt()
        if self.interface_context:
            system = self.interface_context + "\n\n" + system
        try:
            resp = self.iris.llm.invoke(
                [SystemMessage(content=system), HumanMessage(content=prompt)]
            )
            return (resp.content or "").strip()
        except Exception as e:
            logger.warning(f"[Diario] Error al redactar: {e}")
            return ""

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
