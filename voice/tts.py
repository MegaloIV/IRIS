"""
voice/tts.py
Text-to-Speech con Kokoro TTS.
Con onnxruntime-gpu instalado usa GPU automáticamente.
Traduce español → inglés antes de sintetizar.
"""

import logging
import os
import tempfile
import threading
import queue
import re
import numpy as np

from config.settings import settings


def _split_sentences(text: str) -> list[str]:
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in sentences if s.strip()]


class TTSEngine:

    def __init__(self):
        self.voice      = settings.voice.kokoro_voice
        self.speed      = settings.voice.kokoro_speed
        self._kokoro    = None
        self._trans_llm = None
        self._init_model()
        self._init_translator()

    def _init_model(self):
        try:
            from kokoro_onnx import Kokoro
            logging.info("[TTS] Cargando Kokoro TTS...")
            self._kokoro = Kokoro(
                "data/kokoro/kokoro-v0_19.fp16.onnx",
                "data/kokoro/voices.bin",
            )
            logging.info(f"[TTS] Kokoro listo — voz: {self.voice}")
        except ImportError:
            raise ImportError("Instala kokoro: pip install kokoro-onnx")
        except Exception as e:
            raise RuntimeError(f"[TTS] Error cargando Kokoro: {e}")

    def _init_translator(self):
        from core.llm_factory import get_analysis_llm
        self._trans_llm = get_analysis_llm()

    def _translate(self, text: str) -> str:
        try:
            from config.prompts import TRANSLATION_PROMPT
            response = self._trans_llm.invoke(TRANSLATION_PROMPT.format(text=text))
            return response.content.strip()
        except Exception as e:
            logging.error(f"[TTS] Error traduciendo: {e}")
            return text

    def _synthesize(self, text_en: str) -> tuple[np.ndarray, int]:
        samples, sr = self._kokoro.create(
            text_en,
            voice = self.voice,
            speed = self.speed,
            lang  = "en-us",
        )
        return samples, sr

    def speak(self, text_es: str):
        if not text_es.strip():
            return

        sentences   = _split_sentences(text_es)
        audio_queue = queue.Queue()
        stop_signal = object()

        def process_all():
            for sentence in sentences:
                try:
                    en          = self._translate(sentence)
                    samples, sr = self._synthesize(en)
                    audio_queue.put((samples, sr))
                except Exception as e:
                    logging.error(f"[TTS] Error procesando: {e}")
            audio_queue.put(stop_signal)

        def play_all():
            try:
                import sounddevice as sd
                while True:
                    item = audio_queue.get()
                    if item is stop_signal:
                        break
                    samples, sr = item
                    sd.play(samples, sr)
                    sd.wait()
            except Exception as e:
                logging.error(f"[TTS] Error reproduciendo: {e}")

        threading.Thread(target=process_all, daemon=True).start()
        play_all()

    def synthesize_for_telegram(self, text_es: str) -> str:
        try:
            import soundfile as sf
            text_en     = self._translate(text_es)
            samples, sr = self._synthesize(text_en)

            tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp_wav.close()
            sf.write(tmp_wav.name, samples, sr)

            tmp_ogg = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
            tmp_ogg.close()
            try:
                import subprocess
                subprocess.run(
                    ["ffmpeg", "-i", tmp_wav.name, "-c:a", "libopus", tmp_ogg.name, "-y"],
                    check=True, capture_output=True
                )
                os.unlink(tmp_wav.name)
                return tmp_ogg.name
            except Exception:
                os.unlink(tmp_ogg.name)
                return tmp_wav.name
        except Exception as e:
            logging.error(f"[TTS] Error para Telegram: {e}")
            return ""