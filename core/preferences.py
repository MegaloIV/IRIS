"""
core/preferences.py
Gustos y aversiones que Iris se forma sola.

Es el único estado de Iris que se acumula por experiencia en vez de recalcularse
en cada turno. El mood responde al mensaje de ahora; la energía sube y baja sola;
el trust crece de forma monótona. Una preferencia, en cambio, se forma en un
momento concreto, se refuerza o se corrige con lo que va pasando, y se desvanece
si no vuelve a aparecer.

Dos decisiones que conviene entender antes de tocar los números:

1. El refuerzo tiene rendimientos decrecientes. La primera vez que algo te pasa
   te forma una opinión; la décima apenas la mueve. Sin eso, la última
   conversación siempre gana y no hay nada estable.

2. El decaimiento se CALCULA, no se guarda. `strength` en la base de datos es la
   fuerza en el momento del último refuerzo; la fuerza actual se deriva del
   tiempo transcurrido. Así no hay que llevar la cuenta de cuándo se aplicó el
   decaimiento por última vez, ni hay riesgo de aplicarlo dos veces.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


def _parse_ts(value) -> datetime:
    """Acepta datetime (Postgres) o str ISO (SQLite)."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            pass
    return datetime.now()


@dataclass
class Preference:
    subject: str
    kind: str = "tema"
    valence: float = 0.0
    strength: float = 0.0          # fuerza en el último refuerzo, no la actual
    formed_at: Optional[str] = None
    last_reinforced: Optional[str] = None
    evidence: list = field(default_factory=list)

    # ─── Decaimiento derivado ─────────────────────────────────────────────────

    DECAY_PER_DAY = 0.985          # media vida ≈ 46 días

    @property
    def age_days(self) -> float:
        return (datetime.now() - _parse_ts(self.last_reinforced)).total_seconds() / 86400

    @property
    def current_strength(self) -> float:
        """Fuerza ahora mismo, ya descontado el tiempo sin reforzar."""
        days = self.age_days
        if days <= 0:
            return self.strength
        return self.strength * (self.DECAY_PER_DAY ** days)

    @property
    def intensity(self) -> str:
        v = abs(self.valence)
        if v >= 0.7: return "fuerte"
        if v >= 0.4: return "claro"
        return "leve"

    def to_dict(self) -> dict:
        return {
            "subject":         self.subject,
            "kind":            self.kind,
            "valence":         self.valence,
            "strength":        self.strength,
            "formed_at":       self.formed_at,
            "last_reinforced": self.last_reinforced,
            "evidence":        self.evidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Preference":
        return cls(
            subject         = data["subject"],
            kind            = data.get("kind", "tema"),
            valence         = float(data.get("valence", 0.0)),
            strength        = float(data.get("strength", 0.0)),
            formed_at       = data.get("formed_at"),
            last_reinforced = data.get("last_reinforced"),
            evidence        = data.get("evidence") or [],
        )


class PreferenceEngine:
    """
    Mantiene las preferencias en memoria y las escribe al storage al cambiarlas.

    Son pocas (decenas) y se consultan en cada turno, así que viven en RAM —
    mismo patrón que EmotionalState.
    """

    REINFORCE_RATE    = 0.15   # cuánto sube strength con cada evidencia nueva
    FORGET_BELOW      = 0.05   # por debajo de esto se olvida y se borra la fila
    SIGNIFICANT_ABOVE = 0.25   # por debajo de esto no es un rasgo, es ruido
    MAX_EVIDENCE      = 5      # solo se guardan los momentos más recientes

    def __init__(self, storage):
        self._storage = storage
        self._prefs: dict[str, Preference] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            rows = self._storage.get_all()
        except Exception as e:
            logging.warning(f"[Preferences] No pude cargar preferencias: {e}")
            rows = []
        self._prefs = {r["subject"]: Preference.from_dict(r) for r in rows}
        if self._prefs:
            logging.info(f"[Preferences] {len(self._prefs)} cargadas.")

    # ─── Formación ────────────────────────────────────────────────────────────

    def reinforce(self, subject: str, kind: str, valence: float, why: str = "") -> Optional[Preference]:
        """
        Registra una reacción nueva sobre `subject`.

        Si ya existía, la valencia se mueve hacia la nueva observación con un
        peso que decrece según la preferencia está más asentada: una impresión
        nueva mueve mucho una opinión incipiente y casi nada una consolidada.
        """
        subject = (subject or "").strip().lower()
        if not subject:
            return None

        try:
            valence = max(-1.0, min(1.0, float(valence)))
        except (TypeError, ValueError):
            return None

        now = datetime.now().isoformat()

        with self._lock:
            pref = self._prefs.get(subject)

            if pref is None:
                pref = Preference(
                    subject   = subject,
                    kind      = kind or "tema",
                    valence   = valence,
                    strength  = self.REINFORCE_RATE,
                    formed_at = now,
                )
            else:
                # Parte de la fuerza ya decaída, no de la del último refuerzo:
                # una preferencia medio olvidada vuelve a ser maleable.
                base = pref.current_strength
                w    = 1.0 / (1.0 + base * 3.0)
                pref.valence  = pref.valence * (1.0 - w) + valence * w
                pref.strength = min(1.0, base + self.REINFORCE_RATE * (1.0 - base))
                pref.kind     = kind or pref.kind

            pref.last_reinforced = now
            if why:
                pref.evidence = (pref.evidence + [{"when": now[:10], "why": why}])[-self.MAX_EVIDENCE:]

            self._prefs[subject] = pref

        try:
            self._storage.save(pref.to_dict())
        except Exception as e:
            logging.warning(f"[Preferences] No pude guardar '{subject}': {e}")

        return pref

    def reinforce_many(self, reactions: list[dict]) -> int:
        """Aplica una tanda de reacciones. Devuelve cuántas se registraron."""
        n = 0
        for r in reactions or []:
            if not isinstance(r, dict):
                continue
            pref = self.reinforce(
                subject = r.get("subject", ""),
                kind    = r.get("kind", "tema"),
                valence = r.get("valence", 0.0),
                why     = r.get("why", ""),
            )
            if pref:
                n += 1
                logging.info(
                    f"[Preferences] {pref.subject!r} → valence {pref.valence:+.2f} "
                    f"(fuerza {pref.current_strength:.2f})"
                )
        return n

    # ─── Mantenimiento ────────────────────────────────────────────────────────

    def prune(self) -> int:
        """Olvida las preferencias que se apagaron. Devuelve cuántas se borraron."""
        with self._lock:
            gone = [s for s, p in self._prefs.items() if p.current_strength < self.FORGET_BELOW]
            for s in gone:
                del self._prefs[s]

        for s in gone:
            try:
                self._storage.delete(s)
                logging.info(f"[Preferences] Olvidada: {s!r}")
            except Exception as e:
                logging.warning(f"[Preferences] No pude borrar '{s}': {e}")
        return len(gone)

    # ─── Lectura ──────────────────────────────────────────────────────────────

    def get(self, subject: str) -> Optional[Preference]:
        return self._prefs.get((subject or "").strip().lower())

    def all(self) -> list[Preference]:
        """Todas, de más a menos fuerte ahora mismo."""
        return sorted(self._prefs.values(), key=lambda p: p.current_strength, reverse=True)

    def significant(self) -> list[Preference]:
        """Las que ya son un rasgo y no ruido de una sola evidencia."""
        return [p for p in self.all() if p.current_strength >= self.SIGNIFICANT_ABOVE]

    def count(self) -> int:
        return len(self._prefs)

    # ─── Presentación ─────────────────────────────────────────────────────────

    def describe_for_prompt(self, limit: int = 8) -> str:
        """
        Bloque de gustos para el system prompt.

        Devuelve "" mientras no haya ninguna preferencia asentada, así que
        conectarlo antes de que se forme nada es inofensivo: no aparece.

        No se empareja la petición del usuario con un subject concreto — se le
        da la lista entera y el modelo conecta solo. Emparejar "resume este pdf"
        con "leer pdfs largos" es exactamente lo que un LLM hace bien, y hacerlo
        en código costaría otra llamada o un matcher frágil.
        """
        prefs = self.significant()
        if not prefs:
            return ""

        lines = []
        for p in prefs[:limit]:
            signo = "te gusta" if p.valence > 0 else "te desagrada"
            desde = ""
            if p.formed_at:
                dias = (datetime.now() - _parse_ts(p.formed_at)).days
                if dias >= 14:
                    desde = f", desde hace {dias // 7} semanas"
            lines.append(f"- {p.subject}: {signo} ({p.intensity}{desde})")

        return (
            "TUS GUSTOS Y AVERSIONES:\n"
            + "\n".join(lines)
            + "\nSe formaron solos, con lo que ha ido pasando entre ustedes — son tuyos y "
              "son reales, no te los estás inventando. Tiñen cómo respondes: te enciendes "
              "más con lo que te gusta y arrastras los pies con lo que no. Pero son un "
              "sesgo, no un guion: no los anuncies ni los recites, y no hace falta que "
              "expliques por qué algo te da pereza."
        )

    def summary(self) -> str:
        """Vista para el comando /gustos."""
        prefs = self.all()
        if not prefs:
            return "Todavía no me he formado ningún gusto."

        lines = []
        for p in prefs:
            fuerza = p.current_strength
            marca  = "  " if fuerza >= self.SIGNIFICANT_ABOVE else "· "  # · = aún no cuenta
            barra  = "█" * max(1, round(fuerza * 10))
            signo  = "+" if p.valence >= 0 else "−"
            dias   = int(p.age_days)
            visto  = "hoy" if dias == 0 else f"hace {dias}d"
            lines.append(
                f"{marca}{signo}{abs(p.valence):.2f} {barra:<10} {p.subject}  "
                f"[{p.kind} · {visto}]"
            )
            if p.evidence:
                lines.append(f"      └ {p.evidence[-1].get('why', '')[:70]}")

        n_sig = len(self.significant())
        cab   = f"{len(prefs)} preferencias ({n_sig} ya cuentan como rasgo, · = todavía ruido):"
        return cab + "\n" + "\n".join(lines)
