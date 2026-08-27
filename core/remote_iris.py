"""
core/remote_iris.py
Iris vista desde el portátil cuando el cerebro está en el servidor.

Expone la misma superficie que IrisAgent en lo que la UI y la voz usan, así que
main.py, el avatar y VoiceListener funcionan igual sin enterarse de nada. Por
dentro no hay ni personalidad ni memoria: todo eso vive en el otro lado, y aquí
solo se manda y se recibe.

Lo importante de esto: en modo cliente NO se instancia IrisAgent. Dos agentes
contra la misma base de datos se pisarían el estado emocional (que es una única
fila) y entrelazarían el historial.
"""

import json
import logging
import threading
from typing import Callable, Optional

import requests

from config.settings import settings

logger = logging.getLogger(__name__)


class _RemoteMood:
    """Lo mínimo para que el avatar pueda leer `iris.personality.state.mood.value`."""
    def __init__(self):
        self.value = "neutral"


class _RemoteState:
    def __init__(self):
        self.mood   = _RemoteMood()
        self.energy = 100.0
        self.trust_level = 0.0


class _RemotePersonality:
    def __init__(self):
        self.state = _RemoteState()

    def get_status_summary(self) -> str:
        return f"Mood: {self.state.mood.value} | (estado en el servidor)"


class RemoteIris:
    """Cliente HTTP/WS contra el servidor de Iris."""

    def __init__(self):
        self.base = settings.mode.server_url.rstrip("/")
        self.personality = _RemotePersonality()
        self._voice: Optional[object] = None
        self._session = requests.Session()
        self._session.headers["X-Iris-Token"] = settings.mode.agent_token

    # ─── Conversación ─────────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict, timeout: int = 300) -> dict:
        r = self._session.post(f"{self.base}{path}", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _remember_mood(self, data: dict) -> None:
        mood = data.get("mood")
        if mood:
            self.personality.state.mood.value = mood

    def chat(self, user_input: str, interface_context: str = "") -> str:
        data = self._post("/chat", {
            "message": user_input,
            "interface_context": interface_context,
            "delegate": False,
        })
        self._remember_mood(data)
        return data.get("response", "...")

    def delegate_to_claude(self, user_input: str, file_path: str = None,
                           on_delegating: Callable = None, interface_context: str = "") -> str:
        if on_delegating:
            on_delegating()
        data = self._post("/chat", {
            "message": user_input,
            "interface_context": interface_context,
            "delegate": True,
            "file_path": file_path,
        })
        self._remember_mood(data)
        return data.get("response", "...")

    def chat_stream_voice(self, user_input: str, on_sentence: Callable[[str], None]) -> str:
        return self._stream({"message": user_input}, on_sentence)

    def chat_stream_audio(self, wav: bytes, on_sentence: Callable[[str], None]) -> str:
        """
        Manda el audio crudo y recibe la respuesta frase a frase.

        El portátil solo graba y detecta el silencio; transcribir lo hace el
        servidor con la API de Groq, así que aquí no hay ningún modelo de voz
        cargado en memoria.
        """
        import base64
        return self._stream({"audio_b64": base64.b64encode(wav).decode()}, on_sentence)

    def _stream(self, payload: dict, on_sentence: Callable[[str], None]) -> str:
        from websockets.sync.client import connect

        url = self.base.replace("https://", "wss://").replace("http://", "ws://") + "/stream"
        full = ""
        try:
            with connect(url, additional_headers={"X-Iris-Token": settings.mode.agent_token},
                         max_size=64 * 1024 * 1024) as ws:
                ws.send(json.dumps(payload))
                while True:
                    msg = json.loads(ws.recv(timeout=300))
                    kind = msg.get("type")
                    if kind == "sentence":
                        on_sentence(msg.get("text", ""))
                    elif kind == "transcript":
                        logger.info(f"[Voz] Tú: {msg.get('text','')!r}")
                    elif kind == "done":
                        full = msg.get("text", "")
                        self._remember_mood(msg)
                        break
                    elif kind == "error":
                        full = msg.get("text", "[error del servidor]")
                        break
        except Exception as e:
            logger.error(f"[RemoteIris] Stream falló: {e}")
            full = "[No pude hablar con el servidor.]"
        return full

    # ─── Comandos ─────────────────────────────────────────────────────────────

    def run_command(self, cmd: str) -> str:
        """
        Los comandos se ejecutan en el servidor: /memoria, /gustos y /status
        leen estado que solo existe allí.
        """
        try:
            return self._post("/command", {"command": cmd}, timeout=60).get("output", "")
        except Exception as e:
            return f"[No pude alcanzar el servidor: {e}]"

    def get_status(self) -> dict:
        try:
            st = self._session.get(f"{self.base}/status", timeout=30).json()
            mood = st.get("mood")
            if mood:
                self.personality.state.mood.value = mood
            return st
        except Exception as e:
            return {"error": str(e)}

    # ─── Voz (local: micro y altavoz están aquí) ──────────────────────────────

    def start_voice(self, on_speaking_sentence=None, on_listening_changed=None):
        from voice.listener import VoiceListener
        self._voice = VoiceListener(
            on_text_input        = self.chat_stream_voice,
            on_audio_input       = self.chat_stream_audio,   # ← el audio va al servidor
            on_speaking_sentence = on_speaking_sentence,
            on_listening_changed = on_listening_changed,
        )
        self._voice.start()
        logger.info("[RemoteIris] Voz activa — transcripción en el servidor.")

    def stop_voice(self):
        if self._voice:
            self._voice.stop()

    def set_tts_enabled(self, enabled: bool):
        if self._voice:
            self._voice.tts_enabled = enabled

    def speak(self, text: str):
        if self._voice:
            self._voice.speak(text)

    # ─── Ciclo de vida ────────────────────────────────────────────────────────

    def reset_conversation(self):
        self.run_command("/reset")

    def shutdown(self):
        self.stop_voice()
        self._session.close()
