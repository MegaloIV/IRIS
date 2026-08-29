"""
voice/tts.py
Text-to-Speech con ElevenLabs (multilingüe).
Incluye rotación automática de API Keys.
"""

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import queue
import re
import numpy as np
import requests

from config.settings import settings


def _split_sentences(text: str) -> list[str]:
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return [s.strip() for s in sentences if s.strip()]


class TTSEngine:

    def __init__(self):
        # Cargar keys
        keys_env = settings.voice.elevenlabs_keys
        self.api_keys = [k.strip() for k in keys_env.split(",") if k.strip()]
        
        # Cargar IDs de voz
        ids_env = settings.voice.elevenlabs_voice_ids
        self.voice_ids = [v.strip() for v in ids_env.split(",") if v.strip()]
        
        self.current_key_index = 0

        # La voz NO se indexa con la clave. Antes se usaba current_key_index
        # para elegir voice_id, así que agotar una clave le cambiaba la voz a
        # Iris a mitad de respuesta. Son dos cosas independientes: la clave es
        # con qué cuenta pagas, la voz es quién suena.
        self.voice_id = self.voice_ids[0] if self.voice_ids else ""
        if len(self.voice_ids) > 1:
            logging.info(
                f"[TTS] {len(self.voice_ids)} voice IDs configurados; se usa el primero. "
                "Deja solo uno en ELEVENLABS_VOICE_IDS si quieres evitar la ambigüedad."
            )

        if not self.api_keys:
            logging.warning("[TTS] ADVERTENCIA: No se configuraron ELEVENLABS_KEYS en el .env")
        else:
            logging.info(f"[TTS] ElevenLabs listo con {len(self.api_keys)} API keys cargadas.")

        self.ffmpeg_available = shutil.which("ffmpeg") is not None
        if not self.ffmpeg_available:
            logging.warning("[TTS] ffmpeg no encontrado — síntesis de audio para Telegram no disponible. Instálalo con: winget install ffmpeg")

    def _get_current_key(self) -> str:
        if not self.api_keys:
            return ""
        return self.api_keys[self.current_key_index]

    def _rotate_key(self):
        self.current_key_index += 1
        if self.current_key_index >= len(self.api_keys):
            self.current_key_index = 0  # resetear para que la próxima llamada no crashee
            raise Exception(
                "Ninguna API key de ElevenLabs funcionó. Mira el aviso de arriba: "
                "401 = clave inválida o revocada (hay que renovarla), "
                "402 = sin créditos, 429 = límite de peticiones por ahora."
            )
        logging.info(f"[TTS] Rotando a API Key {self.current_key_index + 1}/{len(self.api_keys)}")

    def _synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """Sintetiza audio y devuelve un array NumPy y el sample rate."""
        while True:
            api_key = self._get_current_key()

            # Pedimos formato pcm_24000 para que sea compatible directo con sounddevice
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}?output_format=pcm_24000"
            
            headers = {
                "Accept": "audio/pcm",
                "Content-Type": "application/json",
                "xi-api-key": api_key
            }
            
            data = {
                "text": text,
                "model_id": "eleven_multilingual_v2", # Soporta español nativo
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75
                }
            }

            try:
                # Sin timeout, una API colgada dejaba el hilo de TTS bloqueado
                # para siempre e Iris se quedaba muda sin dar ningún error.
                response = requests.post(url, json=data, headers=headers, timeout=(5, 45))

                if response.status_code == 200:
                    # Convertir los bytes PCM de 16 bits a float32 (lo que espera sounddevice)
                    audio_data = np.frombuffer(response.content, dtype=np.int16)
                    audio_float = audio_data.astype(np.float32) / 32768.0
                    return audio_float, 24000
                
                elif response.status_code in [401, 402, 429]:
                    # Lo que dice ElevenLabs, no lo que yo supongo. Un 401 aquí
                    # tanto puede ser una clave revocada como una voz clonada que
                    # tu plan no permite, y adivinar mandó a buscar tres veces al
                    # sitio equivocado.
                    try:
                        d = response.json().get("detail", {})
                        detalle = d.get("message") or d.get("status") or ""
                    except Exception:
                        detalle = (response.text or "")[:120]
                    # Los tres rotan, pero no significan lo mismo y decirlo mal
                    # manda a buscar el problema donde no está: un 401 no se
                    # arregla esperando a que se renueve la cuota.
                    motivo = {401: "no autorizada",
                              402: "sin créditos",
                              429: "límite de peticiones"}[response.status_code]
                    logging.warning(
                        f"[TTS] Key {self.current_key_index + 1} rechazada — "
                        f"{motivo} (HTTP {response.status_code}). Rotando..."
                        + (f"\n       ElevenLabs dice: {detalle}" if detalle else "")
                    )
                    self._rotate_key()
                else:
                    logging.error(f"[TTS] Error API ElevenLabs ({response.status_code}): {response.text}")
                    return np.array([]), 24000
                    
            except requests.Timeout:
                logging.error("[TTS] ElevenLabs no respondió a tiempo — se omite esta frase.")
                return np.array([]), 24000
            except Exception as e:
                if "agotado" in str(e).lower():
                    raise e
                logging.error(f"[TTS] Error de red al contactar ElevenLabs: {e}")
                return np.array([]), 24000

    def speak(self, text_es: str):
        if not text_es.strip():
            return

        # Ya no necesitamos traducir, pasamos el español directo
        sentences   = _split_sentences(text_es)
        audio_queue = queue.Queue()
        stop_signal = object()

        def process_all():
            for sentence in sentences:
                try:
                    samples, sr = self._synthesize(sentence)
                    if len(samples) > 0:
                        audio_queue.put((samples, sr))
                except Exception as e:
                    logging.error(f"[TTS] Error: {e}")
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

        # Sintetizamos y reproducimos en paralelo para menor latencia
        threading.Thread(target=process_all, daemon=True).start()
        play_all()

    def synthesize_for_telegram(self, text_es: str) -> str:
        """
        Sintetiza el texto completo como un único audio OGG/Opus para Telegram.

        - Divide en oraciones y sintetiza cada una (misma estrategia que speak()).
        - Concatena los arrays de audio antes de escribir el archivo.
        - El WAV intermedio siempre se elimina (finally).
        - El OGG solo se devuelve si la conversión fue exitosa; en cualquier
          error retorna "" para que el bot caiga al fallback de texto.
        - Si ffmpeg no está disponible retorna "" inmediatamente.
        """
        if not self.ffmpeg_available:
            logging.warning("[TTS] ffmpeg no disponible — enviando respuesta como texto")
            return ""

        tmp_wav_path: str | None = None
        tmp_ogg_path: str | None = None

        try:
            import soundfile as sf

            sentences = _split_sentences(text_es) or [text_es]
            chunks: list[np.ndarray] = []
            for sentence in sentences:
                samples, _ = self._synthesize(sentence)
                if len(samples) > 0:
                    chunks.append(samples)

            if not chunks:
                return ""

            combined = np.concatenate(chunks)

            tmp_wav      = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp_wav_path = tmp_wav.name
            tmp_wav.close()

            tmp_ogg      = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
            tmp_ogg_path = tmp_ogg.name
            tmp_ogg.close()

            sf.write(tmp_wav_path, combined, 24000)
            subprocess.run(
                ["ffmpeg", "-i", tmp_wav_path, "-c:a", "libopus", tmp_ogg_path, "-y"],
                check=True,
                capture_output=True,
            )
            return tmp_ogg_path  # el caller (telegram_bot) es responsable de borrarlo

        except Exception as e:
            logging.error(f"[TTS] Error sintetizando audio para Telegram: {e}")
            if tmp_ogg_path:
                try:
                    os.unlink(tmp_ogg_path)
                except Exception:
                    pass
            return ""

        finally:
            # El WAV es siempre un archivo intermedio — se elimina pase lo que pase
            if tmp_wav_path:
                try:
                    os.unlink(tmp_wav_path)
                except Exception:
                    pass