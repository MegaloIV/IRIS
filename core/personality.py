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

from config.settings import settings
from config.prompts import (
    BASE_PERSONALITY, TRUST_STAGES, MOOD_MODIFIERS,
    FEW_SHOT_EXAMPLES, RULES, INPUT_ANALYSIS_PROMPT,
)
import json


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

    def to_dict(self) -> dict:
        return {
            "mood":             self.mood.value,
            "trust_level":      self.trust_level,
            "energy":           self.energy,
            "irritation_count": self.irritation_count,
            "last_interaction": self.last_interaction,
            "inside_jokes":     self.inside_jokes,
            "owner_nickname":   self.owner_nickname,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EmotionalState":
        obj = cls()
        obj.mood             = Mood(data.get("mood", "neutral"))
        obj.trust_level      = data.get("trust_level", 10.0)
        obj.energy           = data.get("energy", 80.0)
        obj.irritation_count = data.get("irritation_count", 0)
        obj.last_interaction = data.get("last_interaction")
        obj.inside_jokes     = data.get("inside_jokes", [])
        obj.owner_nickname   = data.get("owner_nickname")
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
        self.save_state()

    def _apply_time_decay(self):
        if not self.state.last_interaction:
            return
        last        = datetime.fromisoformat(self.state.last_interaction)
        days_silent = (datetime.now() - last).days
        if days_silent >= self.decay_days:
            periods = days_silent // self.decay_days
            self.adjust_trust(-(periods * self.decay_amount), f"{days_silent} días sin hablar")

    def record_interaction(self):
        self.state.last_interaction = datetime.now().isoformat()
        self.adjust_trust(2.0, "interacción normal")

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
            content  = response.content.strip().replace("```json", "").replace("```", "").strip()
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
        self.save_state()

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

        base        = BASE_PERSONALITY.format(owner_name=name, address=address)
        trust_block = TRUST_STAGES[stage.value].format(trust=trust, address=address)
        mood_block  = MOOD_MODIFIERS[mood.value]

        jokes_block = ""
        if self.state.inside_jokes:
            jokes_str   = ", ".join(f'"{j}"' for j in self.state.inside_jokes[-5:])
            jokes_block = f"\nCHISTES INTERNOS: {jokes_str}. Referencialos naturalmente si viene al caso."

        return base + "\n" + trust_block + mood_block + jokes_block + FEW_SHOT_EXAMPLES + RULES

    def get_status_summary(self) -> str:
        stage = self.get_trust_stage()
        return (
            f"Mood: {self.state.mood.value} | "
            f"Trust: {self.state.trust_level:.1f}/100 ({stage.value}) | "
            f"Energy: {self.state.energy:.0f}"
        )