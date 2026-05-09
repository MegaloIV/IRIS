"""
voice/toggle.py
Toggle de escucha de Iris usando el boton Copilot (F23).
Ignora activaciones multiples si ya hay una sesion activa.
"""

import logging
import threading
from typing import Callable, Optional

from config.settings import settings


class VoiceToggle:

    def __init__(self, on_activated: Callable, on_deactivated: Optional[Callable] = None):
        self.on_activated   = on_activated
        self.on_deactivated = on_deactivated
        self.toggle_key     = settings.voice.wake_word
        self._running       = False
        self.listening      = False
        self._thread        = None
        self._init()

    def _init(self):
        try:
            from pynput import keyboard
            self._keyboard = keyboard
            logging.info(f"[Toggle] Boton configurado: {self.toggle_key}")
        except ImportError:
            raise ImportError("Instala pynput: pip install pynput")

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        print("[Toggle] Presiona el boton Copilot para activar/desactivar la escucha.")

    def stop(self):
        self._running  = False
        self.listening = False

    def _listen_loop(self):
        from pynput import keyboard

        try:
            target_key = getattr(keyboard.Key, self.toggle_key)
        except AttributeError:
            logging.error(f"[Toggle] Tecla no reconocida: {self.toggle_key}")
            return

        def on_press(key):
            if not self._running:
                return
            if key == target_key:
                self.listening = not self.listening
                if self.listening:
                    print("\n[Toggle] Iris escuchando...")
                    self.on_activated()
                else:
                    print("\n[Toggle] Iris en silencio.")
                    if self.on_deactivated:
                        self.on_deactivated()

        with keyboard.Listener(on_press=on_press) as listener:
            while self._running:
                listener.join(timeout=0.1)