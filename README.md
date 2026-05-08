# 📁 Estructura del Proyecto Iris — Completa

```
IRIS/
│
├── .env                              # Secrets — NUNCA al repo
├── .gitignore
├── main.py                           # Entry point
├── requirements.txt
│
├── config/
│   ├── __init__.py
│   ├── settings.py                   # Toda la config desde .env
│   └── prompts.py                    # Todos los prompts
│
├── core/
│   ├── __init__.py
│   ├── agent.py                      # Orquestador LangGraph
│   ├── personality.py                # Estado emocional + trust
│   ├── memory.py                     # STM + LTM vectorial + grafo
│   └── llm_factory.py                # Provider switcher
│
├── storage/
│   ├── __init__.py
│   ├── base.py                       # Interfaces abstractas (ABC)
│   ├── factory.py                    # Inicializa todos los backends
│   ├── supabase.py                   # History + State + Vectores
│   └── neo4j.py                      # Grafo de conocimiento
│
├── voice/
│   ├── __init__.py
│   ├── listener.py                   # Orquestador de voz
│   ├── stt.py                        # Speech-to-Text (Whisper)
│   ├── tts.py                        # Text-to-Speech (Coqui/Edge)
│   ├── wake_word.py                  # Detección "iris" (OpenWakeWord)
│   └── train_wake_word.py            # Script entrenar wake word (1 sola vez)
│
└── data/                             # Generado en runtime — NO al repo
    ├── voices/
    │   └── iris_reference.wav        # Tu audio de referencia para clonar voz
    └── wake_word/
        └── iris.onnx                 # Modelo entrenado (genera train_wake_word.py)
```

---

## 🔗 Flujo principal

```
main.py
  │
  ├── IrisAgent()
  │     ├── StorageFactory()
  │     │     ├── SupabaseHistoryStorage   → PostgreSQL (historial)
  │     │     ├── SupabaseStateStorage     → PostgreSQL (estado emocional)
  │     │     ├── SupabaseVectorStorage    → pgvector (memorias semánticas)
  │     │     └── Neo4jGraphStorage        → Neo4j AuraDB (grafo)
  │     │
  │     ├── PersonalityEngine()
  │     │     ├── EmotionalState           → mood, trust, energy
  │     │     ├── TrustStage               → stranger→bonded
  │     │     └── build_system_prompt()    → prompt dinámico
  │     │
  │     ├── MemoryManager()
  │     │     ├── STM                      → RAM (conversación activa)
  │     │     ├── LTM vectorial            → Supabase pgvector
  │     │     └── LTM grafo                → Neo4j
  │     │
  │     ├── LangGraph (4 nodos)
  │     │     ├── analyze_input            → LLM clasifica el mensaje
  │     │     ├── retrieve_memory          → busca memorias relevantes
  │     │     ├── generate_response        → genera respuesta
  │     │     └── update_state             → guarda en STM + sesión
  │     │
  │     └── VoiceListener() (opcional)
  │           ├── WakeWordDetector         → escucha "iris" en background
  │           ├── STTEngine                → Whisper transcribe
  │           └── TTSEngine                → Coqui/Edge responde con voz
  │
  └── Loop terminal
        ├── /status    → estado actual de Iris
        ├── /memoria   → ver memorias guardadas
        ├── /guardar   → forzar extracción de memorias
        ├── /voz       → activar/desactivar voz
        ├── /reset     → limpiar conversación
        ├── /trust +N  → ajustar trust (debug)
        └── /salir     → shutdown limpio
```

---

## 🔄 Flujo de voz

```
Siempre en background (bajo CPU)
WakeWordDetector escucha micrófono
        │
        │ Detecta "iris"
        ▼
STTEngine graba 5 segundos
        │
        │ Transcribe con Whisper
        ▼
IrisAgent.chat(texto)
        │
        │ Genera respuesta
        ▼
TTSEngine.speak(respuesta)
        │
        ├── edge_tts    → voz neural Microsoft (rápido, sin setup)
        └── coqui_xtts  → voz clonada con iris_reference.wav (mejor calidad)
```

---

## 🔄 Flujo de memoria

```
Conversación activa
        │
        ▼
STM (RAM) — últimos 20 mensajes en contexto
        │
        │ Cada mensaje también se guarda en
        ▼
Supabase (PostgreSQL) — historial persistente
        │
        │ Tras X minutos de silencio (timeout)
        ▼
LLM extrae hechos de la sesión
        │
        ├──▶ pgvector  — hechos semánticos con fecha
        └──▶ Neo4j     — entidades y relaciones
        │
        │ Al reiniciar el programa
        ▼
Carga últimos 40 mensajes de Supabase → reconstruye STM
```

---

## 🌐 Servicios externos

```
Groq API       — LLM principal (llama-3.3-70b) + análisis (llama-3.1-8b-instant)
Supabase       — PostgreSQL + pgvector
Neo4j AuraDB   — Grafo de conocimiento
```

---

## ⚙️ .env completo

```env
# LLM
LLM_PROVIDER=groq
LLM_MODEL=llama-3.3-70b-versatile
LLM_ANALYSIS_MODEL=llama-3.1-8b-instant
LLM_TEMPERATURE=0.85
GROQ_API_KEY=

# Supabase
SUPABASE_URL=
SUPABASE_KEY=
DATABASE_URL=postgresql://postgres:[PASSWORD]@db.xxxx.supabase.co:5432/postgres

# Neo4j AuraDB
NEO4J_URI=neo4j+s://xxxx.databases.neo4j.io
NEO4J_USER=xxxx
NEO4J_PASSWORD=

# Iris
IRIS_OWNER_NAME=Matias
IRIS_TIMEZONE=America/Lima

# Memory
MEMORY_STM_WINDOW=20
MEMORY_STM_PERSIST_MESSAGES=40
MEMORY_SESSION_TIMEOUT_MINUTES=120

# Voice — TTS
TTS_ENGINE=edge_tts
TTS_VOICE_SAMPLE=data/voices/iris_reference.wav
TTS_LANGUAGE=es
TTS_SPEED=1.0
TTS_EDGE_VOICE=es-PE-CamilaNeural

# Voice — STT
STT_MODEL=medium
STT_LANGUAGE=es
STT_DEVICE=cuda

# Voice — Wake word
WAKE_WORD=iris
WAKE_WORD_SENSITIVITY=0.5
WAKE_WORD_MODEL_PATH=data/wake_word/iris.onnx

# Telegram (próxima fase)
TELEGRAM_ENABLED=false
TELEGRAM_TOKEN=
TELEGRAM_USER_ID=

# Server (próxima fase)
SERVER_HOST=0.0.0.0
SERVER_PORT=8000
```

---

## 📋 Archivos que NO van al repo (.gitignore)

```
.env
data/
venv/
__pycache__/
*.pyc
config.yaml        # ya no se usa, puedes borrarlo
```

---

## 🗺️ Fases completadas

- ✅ Fase 1 — Core conversacional (LangGraph + personalidad + trust)
- ✅ Fase 2 — Memoria LTM (Supabase pgvector + Neo4j GraphRAG)
- ✅ Fase 3 — Voz (Whisper STT + Coqui/Edge TTS + OpenWakeWord)
- ⏳ Fase 4 — Avatar 2D
- ⏳ Fase 5 — Herramientas (control del PC, Spotify, VSCode, etc.)
- ⏳ Fase 6 — Telegram Bot
- ⏳ Fase 7 — Proactividad (rutinas, iniciativa propia)
- ⏳ Fase 8 — Deploy Railway + acceso remoto