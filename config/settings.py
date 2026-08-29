"""
config/settings.py
Toda la configuración del proyecto Iris.
"""

from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
import hashlib
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
    api_key: Optional[str] = None       # providers de clave única (anthropic, openai)
    api_keys: list[str] = []            # rotación de claves (groq)
    analysis_model: str = os.getenv("LLM_ANALYSIS_MODEL", "llama-3.1-8b-instant")

    def model_post_init(self, __context):
        # Groq: soporte multi-key con rotación automática
        groq_keys_raw = os.getenv("GROQ_API_KEYS", "")
        if groq_keys_raw:
            self.api_keys = [k.strip() for k in groq_keys_raw.split(",") if k.strip()]
        elif self.provider == "groq":
            single = os.getenv("GROQ_API_KEY", "")
            if single:
                self.api_keys = [single]

        # Providers de clave única
        single_key_vars = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
        env_var = single_key_vars.get(self.provider)
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


class VoiceConfig(BaseModel):
    # TTS — ElevenLabs
    elevenlabs_keys: str = os.getenv("ELEVENLABS_KEYS", "")
    elevenlabs_voice_ids: str = os.getenv("ELEVENLABS_VOICE_IDS", "21m00Tcm4TlvDq8ikWAM") # Rachel por defecto

    # STT — dónde se transcribe
    #   auto  — groq si Iris corre en servidor, faster-whisper si es local
    #   groq  — API, sin modelo cargado en ninguna máquina
    #   local — faster-whisper en esta máquina
    stt_backend: str = os.getenv("STT_BACKEND", "auto").lower()
    stt_remote_model: str = os.getenv("STT_REMOTE_MODEL", "whisper-large-v3-turbo")

    # STT — faster-whisper (solo si stt_backend acaba siendo local)
    stt_model: str = os.getenv("STT_MODEL", "small")
    stt_language: str = os.getenv("STT_LANGUAGE", "es")
    stt_device: str = os.getenv("STT_DEVICE", "cuda")

    # Toggle — botón Copilot
    wake_word: str = os.getenv("WAKE_WORD", "f23")

class ClaudeConfig(BaseModel):
    bin_path: str = os.getenv("CLAUDE_BIN_PATH", "/home/matias/.npm-global/bin/claude")

    # Herramientas que Iris puede usar sin pedir permiso. Sustituye a
    # --dangerously-skip-permissions, que autorizaba también Bash — es decir,
    # cualquier comando que se le ocurriera a un LLM, incluido borrar cosas.
    #
    # Con esta lista puede leer, escribir y buscar archivos, que es lo que
    # necesitan sus tareas reales. Si alguna vez hace falta ejecutar comandos,
    # añade Bash aquí a conciencia, o mejor acotado: Bash(git status *)
    allowed_tools: str = os.getenv(
        "CLAUDE_ALLOWED_TOOLS",
        "Read,Write,Edit,Glob,Grep,WebSearch,WebFetch",
    )


class CompanionConfig(BaseModel):
    enabled: bool = os.getenv("COMPANION_ENABLED", "false").lower() == "true"
    url: str = os.getenv("COMPANION_URL", "http://localhost:7891")
    startup_timeout: int = int(os.getenv("COMPANION_STARTUP_TIMEOUT", "8"))


def _default_public_url() -> str:
    """
    URL por la que se llega a Iris desde fuera. Solo importa en modo servidor.

    Se deriva de IRIS_DOMAIN — el mismo valor que usa Caddy en docker-compose —
    para no tener dos variables que decir lo mismo y poder desincronizarse.
    """
    explicito = os.getenv("IRIS_PUBLIC_URL", "").strip().rstrip("/")
    if explicito:
        return explicito
    dominio = os.getenv("IRIS_DOMAIN", "").strip()
    return f"https://{dominio}" if dominio else ""


class JournalConfig(BaseModel):
    """
    La vida interior: qué hace Iris cuando nadie la mira.

    Los valores por defecto siguen el despliegue por etapas del plan: el diario
    se llena desde el primer día, pero `share_enabled` empieza en false para que
    puedas leer con /diario qué se está formando antes de dejarle interrumpirte.
    Cuando te apetezca, lo activas y vas bajando `impulse_threshold` desde 0.75
    hasta el punto donde te alegra que escriba en vez de molestarte.
    """
    enabled: bool = os.getenv("JOURNAL_ENABLED", "true").lower() == "true"
    # Escribir al diario es barato y privado; mandarte un mensaje no lo es.
    share_enabled: bool = os.getenv("JOURNAL_SHARE_ENABLED", "false").lower() == "true"
    impulse_threshold: float = float(os.getenv("JOURNAL_IMPULSE_THRESHOLD", "0.75"))
    # Minutos entre ticks. Es un rango, no un número: 30 exactos se notan y
    # parecen un cron, que es justo lo contrario de lo que se busca.
    interval_min: int = int(os.getenv("JOURNAL_INTERVAL_MIN", "20"))
    interval_max: int = int(os.getenv("JOURNAL_INTERVAL_MAX", "40"))
    # Pensar cansa. Sin coste, rumiaría sin parar; con él, el ciclo se autorregula.
    energy_cost: float = float(os.getenv("JOURNAL_ENERGY_COST", "4"))
    energy_floor: float = float(os.getenv("JOURNAL_ENERGY_FLOOR", "30"))
    # Las actividades usan Claude en el portátil, o sea cuota de la suscripción.
    max_activities_per_day: int = int(os.getenv("JOURNAL_MAX_ACTIVITIES", "3"))

    def model_post_init(self, __context):
        if self.interval_min > self.interval_max:
            raise ValueError(
                f"JOURNAL_INTERVAL_MIN ({self.interval_min}) no puede ser mayor "
                f"que JOURNAL_INTERVAL_MAX ({self.interval_max})."
            )


def _default_brain_token() -> str:
    """
    Llave del editor del cerebro cuando se sirve desde internet.

    Es un secreto propio y NO el `IRIS_AGENT_TOKEN` a posta: aquel autoriza a
    ejecutar Claude en el portátil con acceso a archivos, y va a viajar por un
    chat de Telegram cada vez que pidas el enlace. Que se filtre uno no debe
    entregar el otro.

    Si no se define, se deriva del token del enlace — así funciona sin
    configurar nada, pero sigue siendo un valor distinto.
    """
    explicito = os.getenv("BRAIN_TOKEN", "").strip()
    if explicito:
        return explicito
    base = os.getenv("IRIS_AGENT_TOKEN", "")
    return hashlib.sha256(("cerebro:" + base).encode()).hexdigest()[:32] if base else ""


class BrainConfig(BaseModel):
    """El editor visual del cerebro."""
    port: int = int(os.getenv("BRAIN_PORT", "8765"))
    token: str = _default_brain_token()
    # Si se sirve desde internet, ¿deja escribir? Se puede apagar para que desde
    # fuera solo se pueda mirar.
    remote_write: bool = os.getenv("BRAIN_REMOTE_WRITE", "true").lower() == "true"


class ServerConfig(BaseModel):
    host: str = os.getenv("SERVER_HOST", "0.0.0.0")
    port: int = int(os.getenv("SERVER_PORT", "8000"))
    public_url: str = _default_public_url()


class ModeConfig(BaseModel):
    """
    Dónde vive Iris.

    local   — todo en un proceso, como siempre. Es el valor por defecto, así que
              nada cambia hasta que tú lo cambies.
    server  — el cerebro: agente, memoria, Telegram. Delega en el portátil lo
              que necesita estar allí (Claude, escritorio) vía WebSocket.
    client  — el portátil: avatar, micro, altavoz, y las capacidades que ofrece
              al servidor. Se conecta hacia fuera; no escucha nada.
    """
    mode: str = os.getenv("IRIS_MODE", "local").lower()
    server_url: str = os.getenv("IRIS_SERVER_URL", "http://localhost:8000")
    agent_token: str = os.getenv("IRIS_AGENT_TOKEN", "")
    agent_name: str = os.getenv("IRIS_AGENT_NAME", "portatil")

    def model_post_init(self, __context):
        if self.mode not in ("local", "client", "server"):
            raise ValueError(
                f"IRIS_MODE='{self.mode}' no es válido. Usa local, client o server."
            )
        if self.mode in ("client", "server") and not self.agent_token:
            raise ValueError(
                f"IRIS_MODE={self.mode} necesita IRIS_AGENT_TOKEN en el .env. "
                "Genera uno con: python -c \"import secrets;print(secrets.token_urlsafe(32))\" "
                "y pon el MISMO valor en las dos máquinas."
            )


_raw_owner_id = os.getenv("TELEGRAM_OWNER_ID", "")


def _default_webhook_secret() -> str:
    """
    Secreto que Telegram devuelve en cada petición al webhook.

    Sin él, la única defensa del endpoint es el `owner_id` que viene DENTRO del
    cuerpo del POST — o sea, un dato que lo pone quien llama. En local, detrás
    de un túnel efímero, casi da igual; con un dominio fijo y público, cualquiera
    que dé con la URL puede fabricar un update diciendo ser tú, y Iris le haría
    caso: leerle archivos, mirar la pantalla, abrir programas.

    Si no se define, se deriva del token del bot. No es un secreto independiente,
    pero quien tenga el token del bot ya controla el bot entero, así que no
    debilita nada — y evita una variable más que rellenar a mano.
    """
    explicito = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if explicito:
        return explicito
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    return hashlib.sha256(token.encode()).hexdigest()[:32] if token else ""


class TelegramConfig(BaseModel):
    enabled: bool = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
    bot_token: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
    owner_id: Optional[int] = int(_raw_owner_id) if _raw_owner_id.lstrip("-").isdigit() else None
    webhook_url: Optional[str] = os.getenv("TELEGRAM_WEBHOOK_URL")
    webhook_secret: str = _default_webhook_secret()
    tts_enabled: bool = os.getenv("TELEGRAM_TTS_ENABLED", "false").lower() == "true"
    stt_enabled: bool = os.getenv("TELEGRAM_STT_ENABLED", "false").lower() == "true"


class Settings(BaseModel):
    iris: IrisConfig = IrisConfig()
    llm: LLMConfig = LLMConfig()
    personality: PersonalityConfig = PersonalityConfig()
    memory: MemoryConfig = MemoryConfig()
    storage: StorageConfig = StorageConfig()
    voice: VoiceConfig = VoiceConfig()
    claude: ClaudeConfig = ClaudeConfig()
    companion: CompanionConfig = CompanionConfig()
    server: ServerConfig = ServerConfig()
    mode: ModeConfig = ModeConfig()
    telegram: TelegramConfig = TelegramConfig()
    journal: JournalConfig = JournalConfig()
    brain: BrainConfig = BrainConfig()


settings = Settings()