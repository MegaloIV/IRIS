"""
voice/stt.py
Speech-to-Text usando faster-whisper.
"""

import logging
import tempfile
import os
import numpy as np

from config.settings import settings


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

    def record_and_transcribe(self, duration: float = 5.0) -> str:
        try:
            import sounddevice as sd
            import soundfile as sf

            sample_rate = 16000
            audio = sd.rec(int(duration * sample_rate), samplerate=sample_rate, channels=1, dtype="float32")
            sd.wait()

            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            sf.write(tmp.name, audio.flatten(), sample_rate)
            result = self.transcribe_file(tmp.name)
            os.unlink(tmp.name)
            return result
        except Exception as e:
            logging.error(f"[STT] Error grabando: {e}")
            return ""