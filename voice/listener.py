"""
voice/listener.py
Orquestador de voz con toggle.

Botón Copilot ON  → STT activo, Iris escucha en loop
Botón Copilot OFF → STT detenido, Discord etc. libre
"""

import logging
import threading
import queue
from typing import Callable

from config.settings import settings
from voice.stt import STTEngine
from voice.tts import TTSEngine
from voice.toggle import VoiceToggle


class VoiceListener:

    def __init__(self, on_text_input: Callable):
        self.on_text_input = on_text_input
        self.stt           = STTEngine()
        self.tts           = TTSEngine()
        self._lock         = threading.Lock()

        self.toggle = VoiceToggle(
            on_activated   = self._on_toggle_on,
            on_deactivated = self._on_toggle_off,
        )

    # ─── Control ──────────────────────────────────────────────────────────────

    def start(self):
        self.toggle.start()
        logging.info("[Voice] Sistema de voz activo.")

    def stop(self):
        self.toggle.stop()
        logging.info("[Voice] Sistema de voz detenido.")

    # ─── Toggle callbacks ─────────────────────────────────────────────────────

    def _on_toggle_on(self):
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def _on_toggle_off(self):
        logging.info("[Voice] STT detenido.")

    # ─── Loop de escucha ──────────────────────────────────────────────────────

    def _listen_loop(self):
        logging.info("[Voice] Loop de escucha iniciado.")

        while self.toggle.listening:
            try:
                text = self.stt.record_and_transcribe(duration=5.0)

                if not self.toggle.listening:
                    break

                if not text:
                    continue

                print(f"\n[Voice] Tú: '{text}'")

                sentence_queue = queue.Queue()
                stop_signal    = object()

                def on_sentence(sentence: str):
                    sentence_queue.put(sentence)

                def llm_thread():
                    try:
                        full = self.on_text_input(text, on_sentence)
                        print(f"[Voice] Iris: '{full}'")
                    except Exception as e:
                        logging.error(f"[Voice] Error LLM: {e}")
                    finally:
                        sentence_queue.put(stop_signal)

                threading.Thread(target=llm_thread, daemon=True).start()

                while True:
                    item = sentence_queue.get()
                    if item is stop_signal:
                        break
                    if self.toggle.listening:
                        try:
                            self.tts.speak(item)
                        except Exception as e:
                            logging.error(f"[Voice] Error TTS: {e}")

            except Exception as e:
                logging.error(f"[Voice] Error en loop: {e}")
                break

        logging.info("[Voice] Loop de escucha detenido.")

    # ─── Utilidades ───────────────────────────────────────────────────────────

    def speak(self, text: str):
        """Para mensajes proactivos — habla sin importar el toggle."""
        self.tts.speak(text)

    def synthesize_for_telegram(self, text: str) -> str:
        return self.tts.synthesize_for_telegram(text)