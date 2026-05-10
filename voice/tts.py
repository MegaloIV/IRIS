"""
voice/tts.py
Text-to-Speech con ElevenLabs (multilingüe).
Incluye rotación automática de API Keys.
"""

import logging
import os
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

        if not self.api_keys:
            logging.warning("[TTS] ADVERTENCIA: No se configuraron ELEVENLABS_KEYS en el .env")
        else:
            logging.info(f"[TTS] ElevenLabs listo con {len(self.api_keys)} API keys cargadas.")

    def _get_current_key(self) -> str:
        if not self.api_keys:
            return ""
        return self.api_keys[self.current_key_index]

    def _rotate_key(self):
        self.current_key_index += 1
        if self.current_key_index >= len(self.api_keys):
            raise Exception("Se han agotado todas las API keys de ElevenLabs (Límite de cuota o bloqueadas).")
        logging.info(f"[TTS] Rotando a API Key {self.current_key_index + 1}/{len(self.api_keys)}")

    def _synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """Sintetiza audio y devuelve un array NumPy y el sample rate."""
        while True:
            api_key = self._get_current_key()
            
            # CORRECCIÓN 1: Obtener el Voice ID que le corresponde a la API Key actual
            if hasattr(self, 'voice_ids') and self.voice_ids:
                current_voice_id = self.voice_ids[self.current_key_index] if self.current_key_index < len(self.voice_ids) else self.voice_ids[0]
            else:
                # Fallback por si acaso no cambiaste el __init__
                current_voice_id = self.voice_id 

            # Pedimos formato pcm_24000 para que sea compatible directo con sounddevice
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{current_voice_id}?output_format=pcm_24000"
            
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
                response = requests.post(url, json=data, headers=headers)

                if response.status_code == 200:
                    # Convertir los bytes PCM de 16 bits a float32 (lo que espera sounddevice)
                    audio_data = np.frombuffer(response.content, dtype=np.int16)
                    audio_float = audio_data.astype(np.float32) / 32768.0
                    return audio_float, 24000
                
                # CORRECCIÓN 2: Se agregó el error 402 a la lista de rotación
                elif response.status_code in [401, 402, 429]:
                    logging.warning(f"[TTS] Falló key {self.current_key_index + 1} (Status {response.status_code}). Rotando...")
                    self._rotate_key()
                else:
                    logging.error(f"[TTS] Error API ElevenLabs ({response.status_code}): {response.text}")
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
        """Sintetiza audio para Telegram en .ogg"""
        try:
            import soundfile as sf
            samples, sr = self._synthesize(text_es)

            if len(samples) == 0:
                return ""

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