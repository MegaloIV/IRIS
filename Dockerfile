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
COPY main.py .

ENV IRIS_MODE=server \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1

EXPOSE 8000
CMD ["python", "main.py"]
