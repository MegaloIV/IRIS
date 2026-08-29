# Iris en modo servidor: el cerebro, sin UI ni voz.
# Lo que necesita estar en el portátil (Claude, escritorio) se lo pide al
# agente por WebSocket — aquí no hay ni suscripción de Claude ni pantalla.
FROM python:3.11-slim

# libgomp: torch lo necesita para el modelo de embeddings
# ffmpeg:  synthesize_for_telegram convierte el PCM de ElevenLabs a OGG/Opus.
#          Sin el, devuelve "" y las respuestas de voz caen a texto sin avisar.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Solo lo que hace falta en el servidor. PyQt6, sounddevice, faster-whisper y
# pyautogui viven en el portátil; instalarlos aquí serían cientos de MB inútiles.
COPY requirements.server.txt .
RUN pip install --no-cache-dir -r requirements.server.txt

# El modelo de embeddings se hornea en la imagen: así el contenedor arranca sin
# salir a HuggingFace, que es lo que local_files_only espera encontrar.
RUN python -c "from sentence_transformers import SentenceTransformer; \
               SentenceTransformer('all-MiniLM-L6-v2')"

COPY config/  ./config/
COPY core/    ./core/
COPY storage/ ./storage/
COPY interfaces/ ./interfaces/
# scripts/: core/startup.py importa de aquí register_webhook. Sin esta línea el
# webhook de Telegram no se registra al arrancar — y falla dentro de un hilo, o
# sea en silencio, que es justo el problema que register_webhook venía a resolver.
COPY scripts/ ./scripts/
# voice/: aquí no se graba ni se reproduce nada, pero el servidor sí TRANSCRIBE.
# El portátil manda el WAV por el WebSocket y las notas de voz de Telegram llegan
# como audio; las dos cosas pasan por voice/stt.py. voice/tts.py sintetiza las
# respuestas de voz de Telegram (por eso ffmpeg más arriba).
COPY voice/ ./voice/
# ui/brain: la página del editor del cerebro. Del resto de ui/ (avatar, ventanas)
# aquí no hay nada que hacer —no hay pantalla— pero esta sí se sirve por HTTP,
# así que el fichero tiene que existir dentro de la imagen.
COPY ui/brain/ ./ui/brain/
COPY main.py .

ENV IRIS_MODE=server \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1

EXPOSE 8000
CMD ["python", "main.py"]
