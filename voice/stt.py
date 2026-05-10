"""
voice/stt.py
Speech-to-Text usando faster-whisper.
Graba con deteccion de silencio.
silence_threshold bajo (0.005) para micros de laptop.
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