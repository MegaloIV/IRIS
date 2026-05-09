"""
voice/listener.py
Orquestador de voz con toggle y deteccion de silencio.
Un solo loop activo a la vez — multiples toques del toggle son ignorados.
"""

import logging
import threading
import queue
from typing import Callable

from voice.stt import STTEngine
from voice.tts import TTSEngine
from voice.toggle import VoiceToggle


class VoiceListener:

    def __init__(self, on_text_input: Callable):
        self.on_text_input = on_text_input
        self.stt           = STTEngine()
        self.tts           = TTSEngine()
        self._loop_lock    = threading.Lock()  # garantiza un solo loop activo

        self.toggle = VoiceToggle(
            on_activated   = self._on_toggle_on,
            on_deactivated = self._on_toggle_off,
        )

    def start(self):
        self.toggle.start()
        logging.info("[Voice] Sistema de voz activo.")

    def stop(self):
        self.toggle.stop()
        logging.info("[Voice] Sistema de voz detenido.")

    def _on_toggle_on(self):
        if self._loop_lock.locked():
            logging.warning("[Voice] Loop ya activo — ignorando activacion extra.")
            return
        threading.Thread(target=self._listen_loop, daemon=True).start()

    def _on_toggle_off(self):
        logging.info("[Voice] Toggle OFF — el loop terminara al detectar silencio.")

    def _listen_loop(self):
        with self._loop_lock:
            logging.info("[Voice] Loop de escucha iniciado.")

            while self.toggle.listening:
                try:
                    text = self.stt.record_and_transcribe(
                        stop_flag = lambda: not self.toggle.listening
                    )

                    if not text:
                        if not self.toggle.listening:
                            break
                        continue

                    print(f"\n[Voice] Tu: {repr(text)}")
                    self._respond(text)

                except Exception as e:
                    logging.error(f"[Voice] Error en loop: {e}")
                    break

            logging.info("[Voice] Loop de escucha detenido.")

    def _respond(self, text: str):
        sentence_queue = queue.Queue()
        stop_signal    = object()

        def on_sentence(sentence: str):
            sentence_queue.put(sentence)

        def llm_thread():
            try:
                full = self.on_text_input(text, on_sentence)
                print(f"[Voice] Iris: {repr(full)}")
            except Exception as e:
                logging.error(f"[Voice] Error LLM: {e}")
            finally:
                sentence_queue.put(stop_signal)

        threading.Thread(target=llm_thread, daemon=True).start()

        while True:
            item = sentence_queue.get()
            if item is stop_signal:
                break
            try:
                self.tts.speak(item)
            except Exception as e:
                logging.error(f"[Voice] Error TTS: {e}")

    def speak(self, text: str):
        """Para mensajes proactivos — siempre habla sin importar toggle."""
        threading.Thread(target=self.tts.speak, args=(text,), daemon=True).start()

    def synthesize_for_telegram(self, text: str) -> str:
        return self.tts.synthesize_for_telegram(text)