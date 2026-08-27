"""
voice/stt.py
Captura de micrófono y transcripción.

La grabación y la detección de silencio son locales siempre — el micrófono está
donde está el usuario, y decidir si alguien ha dejado de hablar es medir el RMS
de unos chunks, que no cuesta nada.

La transcripción es lo que sí pesa, y hay dos caminos:
  STTEngine  — faster-whisper en la máquina. Modelo de 1-2 GB en memoria.
  GroqSTT    — la API de Groq. Nada cargado, unos cientos de ms.

Un detalle que importa: Whisper se inventa texto cuando le das audio sin voz —
un tono puro transcribe como "Gracias.". El faster-whisper local lo filtra con
vad_filter, pero la API no. Por eso `record_utterance()` devuelve None si nunca
detectó voz, y ese None hay que respetarlo antes de mandar nada.
"""

import io
import logging
import tempfile
import os
import wave

import numpy as np

from config.settings import settings


def record_utterance(
    duration: float          = 30.0,
    silence_threshold: float = 0.005,
    silence_duration: float  = 1.5,
    stop_flag                = None,
    sample_rate: int         = 16000,
) -> bytes | None:
    """
    Graba hasta detectar silencio sostenido o stop_flag.

    Devuelve un WAV de 16 kHz mono en memoria, o None si no hubo voz — nunca un
    buffer de silencio, porque Whisper le inventaría palabras.
    """
    try:
        import sounddevice as sd
    except Exception as e:
        logging.error(f"[STT] Sin micrófono disponible: {e}")
        return None

    chunk_size     = int(sample_rate * 0.1)
    silence_chunks = int(silence_duration / 0.1)
    max_chunks     = int(duration / 0.1)

    audio_buffer   = []
    silent_count   = 0
    voice_detected = False

    try:
        with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32") as stream:
            for _ in range(max_chunks):
                if stop_flag and stop_flag():
                    logging.info("[STT] Toggle apagado — procesando lo grabado...")
                    break

                chunk, _   = stream.read(chunk_size)
                chunk_flat = chunk.flatten()
                audio_buffer.append(chunk_flat)

                rms = float(np.sqrt(np.mean(chunk_flat ** 2)))
                if rms > silence_threshold:
                    voice_detected = True
                    silent_count   = 0
                elif voice_detected:
                    silent_count += 1
                    if silent_count >= silence_chunks:
                        logging.info("[STT] Silencio detectado — procesando...")
                        break
    except Exception as e:
        logging.error(f"[STT] Error grabando: {e}")
        return None

    if not voice_detected or not audio_buffer:
        return None

    samples = np.concatenate(audio_buffer)
    pcm16   = np.clip(samples, -1.0, 1.0)
    pcm16   = (pcm16 * 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16.tobytes())
    return buf.getvalue()


class STTEngine:

    def __init__(self):
        self.model_size = settings.voice.stt_model
        self.language   = settings.voice.stt_language
        self.device     = settings.voice.stt_device
        self._model     = None
        self._init_model()

    def _init_model(self):
        try:
            from faster_whisper import WhisperModel
            logging.info(f"[STT] Cargando Whisper {self.model_size} en {self.device}...")
            compute_type = "float16" if self.device == "cuda" else "int8"
            self._model  = WhisperModel(self.model_size, device=self.device, compute_type=compute_type)
            logging.info("[STT] Whisper listo.")
        except Exception as e:
            logging.warning(f"[STT] Fallback a CPU: {e}")
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")

    def transcribe_file(self, audio_path: str) -> str:
        try:
            segments, _ = self._model.transcribe(
                audio_path,
                language       = self.language,
                beam_size      = 5,
                vad_filter     = True,
                vad_parameters = {"min_silence_duration_ms": 500},
            )
            return " ".join(seg.text.strip() for seg in segments).strip()
        except Exception as e:
            logging.error(f"[STT] Error: {e}")
            return ""

    def record_and_transcribe(
        self,
        duration: float          = 30.0,
        silence_threshold: float = 0.005,
        silence_duration: float  = 1.5,
        stop_flag                = None,
        sample_rate: int         = 16000,
    ) -> str:
        """
        Graba hasta detectar silencio sostenido o stop_flag=True.
        stop_flag: callable que retorna True cuando el toggle se apaga.
        """
        try:
            import sounddevice as sd
            import soundfile as sf

            chunk_size     = int(sample_rate * 0.1)
            silence_chunks = int(silence_duration / 0.1)
            max_chunks     = int(duration / 0.1)

            audio_buffer   = []
            silent_count   = 0
            voice_detected = False

            with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32") as stream:
                for _ in range(max_chunks):
                    if stop_flag and stop_flag():
                        logging.info("[STT] Toggle apagado — procesando lo grabado...")
                        break

                    chunk, _ = stream.read(chunk_size)
                    chunk_flat = chunk.flatten()
                    audio_buffer.append(chunk_flat)

                    rms = float(np.sqrt(np.mean(chunk_flat ** 2)))

                    if rms > silence_threshold:
                        voice_detected = True
                        silent_count   = 0
                    elif voice_detected:
                        silent_count += 1
                        if silent_count >= silence_chunks:
                            logging.info("[STT] Silencio detectado — procesando...")
                            break

            if not voice_detected or not audio_buffer:
                return ""

            full_audio = np.concatenate(audio_buffer)
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            sf.write(tmp.name, full_audio, sample_rate)
            result = self.transcribe_file(tmp.name)
            os.unlink(tmp.name)
            return result

        except Exception as e:
            logging.error(f"[STT] Error grabando: {e}")
            return ""

# ─── Transcripción vía Groq ───────────────────────────────────────────────────

class GroqSTT:
    """
    Transcribe con la API de Groq. No carga ningún modelo.

    Es la contraparte de STTEngine para cuando Iris corre en un servidor: el
    portátil graba y manda el WAV, y aquí se convierte en texto sin que ninguna
    de las dos máquinas tenga que sostener un modelo de voz en memoria.
    """

    def __init__(self, model: str = None, language: str = None):
        from groq import Groq
        keys = settings.llm.api_keys
        if not keys:
            raise ValueError("GroqSTT necesita GROQ_API_KEYS")
        self._clients = [Groq(api_key=k) for k in keys]
        self._current = 0
        self.model    = model or settings.voice.stt_remote_model
        self.language = language or settings.voice.stt_language
        logging.info(f"[STT] Groq {self.model} — sin modelo local.")

    def transcribe_bytes(self, wav: bytes) -> str:
        if not wav:
            return ""
        for _ in range(len(self._clients)):
            try:
                r = self._clients[self._current].audio.transcriptions.create(
                    file=("audio.wav", wav),
                    model=self.model,
                    language=self.language,
                    response_format="text",
                )
                return (r if isinstance(r, str) else getattr(r, "text", "")).strip()
            except Exception as e:
                msg = str(e).lower()
                if any(k in msg for k in ("429", "rate", "quota", "limit", "401", "auth")):
                    self._current = (self._current + 1) % len(self._clients)
                    logging.warning(f"[STT] Rotando clave — {type(e).__name__}")
                    continue
                logging.error(f"[STT] Groq falló: {e}")
                return ""
        logging.error("[STT] Todas las claves agotadas.")
        return ""

    def transcribe_file(self, path: str) -> str:
        try:
            with open(path, "rb") as f:
                return self.transcribe_bytes(f.read())
        except Exception as e:
            logging.error(f"[STT] No pude leer {path}: {e}")
            return ""

    def record_and_transcribe(self, **kw) -> str:
        wav = record_utterance(**kw)
        return self.transcribe_bytes(wav) if wav else ""


def build_stt():
    """
    Elige transcriptor según STT_BACKEND.

    groq  — sin modelo local (por defecto en servidor)
    local — faster-whisper (por defecto en local)
    """
    backend = settings.voice.stt_backend
    if backend == "auto":
        backend = "groq" if settings.mode.mode == "server" else "local"
    if backend == "groq":
        return GroqSTT()
    return STTEngine()
