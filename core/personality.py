"""
core/personality.py
Motor de personalidad, estado emocional y sistema de confianza de Iris.
El estado persiste en Supabase via BaseStateStorage.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import logging

import json
import re

from config.settings import settings
from config.prompts import (
    BASE_PERSONALITY, TRUST_STAGES, MOOD_MODIFIERS, ENERGY_MODIFIERS,
    FEW_SHOT_EXAMPLES, RULES, INPUT_ANALYSIS_PROMPT,
)


class Mood(str, Enum):
    NEUTRAL     = "neutral"
    HAPPY       = "happy"
    ANNOYED     = "annoyed"
    CURIOUS     = "curious"
    EMBARRASSED = "embarrassed"
    EXCITED     = "excited"
    BORED       = "bored"
    LONELY      = "lonely"
    FOCUSED     = "focused"


class TrustStage(str, Enum):
    STRANGER     = "stranger"
    ACQUAINTANCE = "acquaintance"
    FRIEND       = "friend"
    CLOSE        = "close"
    BONDED       = "bonded"


@dataclass
class EmotionalState:
    mood: Mood = Mood.NEUTRAL
    trust_level: float = 10.0
    energy: float = 80.0
    irritation_count: int = 0
    last_interaction: Optional[str] = None
    inside_jokes: list = field(default_factory=list)
    owner_nickname: Optional[str] = None
    proactive_sent_today: int = 0
    proactive_last_day: str = ""

    def to_dict(self) -> dict:
        return {
            "mood":                 self.mood.value,
            "trust_level":          self.trust_level,
            "energy":               self.energy,
            "irritation_count":     self.irritation_count,
            "last_interaction":     self.last_interaction,
            "inside_jokes":         self.inside_jokes,
            "owner_nickname":       self.owner_nickname,
            "proactive_sent_today": self.proactive_sent_today,
            "proactive_last_day":   self.proactive_last_day,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EmotionalState":
        obj = cls()
        obj.mood                 = Mood(data.get("mood", "neutral"))
        obj.trust_level          = data.get("trust_level", 10.0)
        obj.energy               = data.get("energy", 80.0)
        obj.irritation_count     = data.get("irritation_count", 0)
        obj.last_interaction     = data.get("last_interaction")
        obj.inside_jokes         = data.get("inside_jokes", [])
        obj.owner_nickname       = data.get("owner_nickname")
        obj.proactive_sent_today = data.get("proactive_sent_today", 0)
        obj.proactive_last_day   = data.get("proactive_last_day", "")
        return obj


class PersonalityEngine:

    def __init__(self, state_storage):
        self.state_storage = state_storage
        self.owner_name    = settings.iris.owner_name
        self.decay_days    = settings.personality.trust_decay_days
        self.decay_amount  = settings.personality.trust_decay_amount
        self.state         = self._load_state()
        self._analysis_llm = None
        self._apply_time_decay()
        self._apply_energy_regen()

    def set_analysis_llm(self, llm):
        self._analysis_llm = llm

    # ─── Persistencia ─────────────────────────────────────────────────────────

    def _load_state(self) -> EmotionalState:
        data = self.state_storage.load()
        if data:
            return EmotionalState.from_dict(data)
        return EmotionalState(trust_level=settings.personality.initial_trust)

    def save_state(self):
        self.state_storage.save(self.state.to_dict())

    # ─── Trust System ──────────────────────────────────────────────────────────

    def get_trust_stage(self) -> TrustStage:
        t = self.state.trust_level
        if t < 20: return TrustStage.STRANGER
        if t < 45: return TrustStage.ACQUAINTANCE
        if t < 70: return TrustStage.FRIEND
        if t < 90: return TrustStage.CLOSE
        return TrustStage.BONDED

    def adjust_trust(self, amount: float, reason: str = ""):
        old = self.state.trust_level
        self.state.trust_level = max(0.0, min(100.0, self.state.trust_level + amount))
        logging.debug(f"[Trust] {old:.1f} → {self.state.trust_level:.1f} ({reason})")

    def _apply_time_decay(self):
        if not self.state.last_interaction:
            return
        last        = datetime.fromisoformat(self.state.last_interaction)
        days_silent = (datetime.now() - last).days
        if days_silent >= self.decay_days:
            periods = days_silent // self.decay_days
            self.adjust_trust(-(periods * self.decay_amount), f"{days_silent} días sin hablar")
            self.save_state()

    def _apply_energy_regen(self):
        """Regenerate energy based on hours elapsed since last interaction (12 pts/hour).
        Also sets mood to LONELY after 4h of silence."""
        if not self.state.last_interaction:
            return
        last         = datetime.fromisoformat(self.state.last_interaction)
        hours_silent = (datetime.now() - last).total_seconds() / 3600
        if hours_silent >= 0.5:
            regen = hours_silent * 12.0
            old   = self.state.energy
            self.state.energy = min(100.0, self.state.energy + regen)
            if self.state.energy > old:
                logging.debug(f"[Energy] Regen +{regen:.1f} tras {hours_silent:.1f}h → {self.state.energy:.1f}")
        if hours_silent >= 4.0 and self.state.mood != Mood.LONELY:
            self.state.mood = Mood.LONELY
            logging.info(f"[Personality] Mood → LONELY ({hours_silent:.1f}h sin interacción)")

    def record_interaction(self):
        self.state.last_interaction = datetime.now().isoformat()
        self.adjust_trust(2.0, "interacción normal")
        self.state.energy = max(0.0, self.state.energy - 3.0)
        logging.debug(f"[Energy] −3 → {self.state.energy:.1f}")

    def on_positive_moment(self):
        self.adjust_trust(5.0, "momento positivo")
        self.state.mood = Mood.HAPPY
        self.state.irritation_count = max(0, self.state.irritation_count - 1)

    def add_inside_joke(self, joke: str):
        if joke not in self.state.inside_jokes:
            self.state.inside_jokes.append(joke)
            self.save_state()

    # ─── Análisis de input ────────────────────────────────────────────────────

    def analyze_input(self, text: str) -> dict:
        if not self._analysis_llm:
            return {}
        try:
            prompt   = INPUT_ANALYSIS_PROMPT.format(text=text)
            response = self._analysis_llm.invoke(prompt)
            content  = re.sub(r'```(?:json)?\s*', '', response.content).strip()
            result   = json.loads(content)
            changes  = {}
            mood_map = {
                "curious": Mood.CURIOUS, "positive": Mood.HAPPY,
                "annoyed": Mood.ANNOYED, "excited": Mood.EXCITED,
                "embarrassed": Mood.EMBARRASSED, "bored": Mood.BORED,
            }
            trigger = result.get("mood_trigger", "neutral")
            if trigger in mood_map:
                changes["mood"] = mood_map[trigger]
            trust_delta = float(result.get("trust_delta", 0))
            if trust_delta != 0:
                changes["trust_delta"] = trust_delta
            if result.get("is_manipulation_attempt", False):
                changes["manipulation_attempt"] = True
            return changes
        except Exception as e:
            logging.warning(f"[Personality] Error análisis: {e}")
            return {}

    def apply_analysis(self, changes: dict):
        if changes.get("mood"):
            self.update_mood(changes["mood"])
        if changes.get("trust_delta"):
            self.adjust_trust(changes["trust_delta"], "análisis de input")
        if changes.get("manipulation_attempt"):
            self.update_mood(Mood.ANNOYED)

    def update_mood(self, new_mood: Mood):
        self.state.mood = new_mood

    # ─── Proactividad ─────────────────────────────────────────────────────────

    def hours_since_last_interaction(self) -> float:
        if not self.state.last_interaction:
            return 0.0
        last = datetime.fromisoformat(self.state.last_interaction)
        return (datetime.now() - last).total_seconds() / 3600

    def can_send_proactive(self, max_per_day: int = 2) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.proactive_last_day != today:
            self.state.proactive_sent_today = 0
            self.state.proactive_last_day   = today
        return self.state.proactive_sent_today < max_per_day

    def record_proactive_sent(self):
        today = datetime.now().strftime("%Y-%m-%d")
        self.state.proactive_last_day    = today
        self.state.proactive_sent_today += 1
        self.save_state()

    # ─── Energy ───────────────────────────────────────────────────────────────

    def get_energy_stage(self) -> str:
        e = self.state.energy
        if e >= 70: return "high"
        if e >= 40: return "medium"
        if e >= 15: return "low"
        return "exhausted"

    # ─── Cómo llama al dueño ──────────────────────────────────────────────────

    def get_owner_address(self) -> str:
        stage = self.get_trust_stage()
        if self.state.owner_nickname and stage in [TrustStage.CLOSE, TrustStage.BONDED]:
            return self.state.owner_nickname
        name = self.owner_name or "tú"
        match stage:
            case TrustStage.STRANGER:     return "usuario"
            case TrustStage.ACQUAINTANCE: return name
            case TrustStage.FRIEND:       return name
            case _:                       return self.state.owner_nickname or name

    # ─── System Prompt ────────────────────────────────────────────────────────

    def build_system_prompt(self) -> str:
        stage   = self.get_trust_stage()
        mood    = self.state.mood
        trust   = self.state.trust_level
        address = self.get_owner_address()
        name    = self.owner_name or "el usuario"

        base         = BASE_PERSONALITY.format(owner_name=name, address=address)
        trust_block  = TRUST_STAGES[stage.value].format(trust=trust, address=address)
        mood_block   = MOOD_MODIFIERS[mood.value]
        energy_block = ENERGY_MODIFIERS[self.get_energy_stage()]

        jokes_block = ""
        if self.state.inside_jokes:
            jokes_str   = ", ".join(f'"{j}"' for j in self.state.inside_jokes[-5:])
            jokes_block = f"\nCHISTES INTERNOS: {jokes_str}. Referencialos naturalmente si viene al caso."

        return base + "\n" + trust_block + mood_block + energy_block + jokes_block + FEW_SHOT_EXAMPLES + RULES

    def get_status_summary(self) -> str:
        stage = self.get_trust_stage()
        return (
            f"Mood: {self.state.mood.value} | "
            f"Trust: {self.state.trust_level:.1f}/100 ({stage.value}) | "
            f"Energy: {self.state.energy:.0f} ({self.get_energy_stage()})"
        )