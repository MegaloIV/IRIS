"""
config/settings.py
Toda la configuración del proyecto Iris.
"""

from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
import os

load_dotenv()


class IrisConfig(BaseModel):
    name: str = "Iris"
    owner_name: str = os.getenv("IRIS_OWNER_NAME", "")
    language: str = "es"
    timezone: str = os.getenv("IRIS_TIMEZONE", "America/Lima")


class LLMConfig(BaseModel):
    provider: str = os.getenv("LLM_PROVIDER", "groq")
    model: str = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.85"))
    api_key: Optional[str] = None
    analysis_model: str = os.getenv("LLM_ANALYSIS_MODEL", "llama-3.1-8b-instant")

    def model_post_init(self, __context):
        keys = {
            "groq":      "GROQ_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "openai":    "OPENAI_API_KEY",
        }
        env_var = keys.get(self.provider)
        if env_var:
            self.api_key = os.getenv(env_var)


class PersonalityConfig(BaseModel):
    initial_trust: float = float(os.getenv("PERSONALITY_INITIAL_TRUST", "10.0"))
    trust_decay_days: int = int(os.getenv("PERSONALITY_DECAY_DAYS", "3"))
    trust_decay_amount: float = float(os.getenv("PERSONALITY_DECAY_AMOUNT", "5.0"))


class MemoryConfig(BaseModel):
    stm_window: int = int(os.getenv("MEMORY_STM_WINDOW", "20"))
    stm_persist_messages: int = int(os.getenv("MEMORY_STM_PERSIST_MESSAGES", "40"))
    session_timeout_minutes: int = int(os.getenv("MEMORY_SESSION_TIMEOUT_MINUTES", "60"))


class StorageConfig(BaseModel):
    database_url: str = os.getenv("DATABASE_URL", "")
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_key: str = os.getenv("SUPABASE_KEY", "")
    neo4j_uri: str = os.getenv("NEO4J_URI", "")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")


class VoiceConfig(BaseModel):
    # TTS — ElevenLabs
    elevenlabs_keys: str = os.getenv("ELEVENLABS_KEYS", "")
    elevenlabs_voice_ids: str = os.getenv("ELEVENLABS_VOICE_IDS", "21m00Tcm4TlvDq8ikWAM") # Rachel por defecto

    # STT — faster-whisper
    stt_model: str = os.getenv("STT_MODEL", "small")
    stt_language: str = os.getenv("STT_LANGUAGE", "es")
    stt_device: str = os.getenv("STT_DEVICE", "cuda")

    # Toggle — botón Copilot
    wake_word: str = os.getenv("WAKE_WORD", "f23")

class ServerConfig(BaseModel):
    host: str = os.getenv("SERVER_HOST", "0.0.0.0")
    port: int = int(os.getenv("SERVER_PORT", "8000"))


_raw_owner_id = os.getenv("TELEGRAM_OWNER_ID", "")


class TelegramConfig(BaseModel):
    enabled: bool = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
    bot_token: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
    owner_id: Optional[int] = int(_raw_owner_id) if _raw_owner_id.lstrip("-").isdigit() else None
    webhook_url: Optional[str] = os.getenv("TELEGRAM_WEBHOOK_URL")
    tts_enabled: bool = os.getenv("TELEGRAM_TTS_ENABLED", "false").lower() == "true"


class Settings(BaseModel):
    iris: IrisConfig = IrisConfig()
    llm: LLMConfig = LLMConfig()
    personality: PersonalityConfig = PersonalityConfig()
    memory: MemoryConfig = MemoryConfig()
    storage: StorageConfig = StorageConfig()
    voice: VoiceConfig = VoiceConfig()
    server: ServerConfig = ServerConfig()
    telegram: TelegramConfig = TelegramConfig()


settings = Settings()