"""
core/llm_factory.py
Crea LLMs según el proveedor configurado en settings.

Para Groq se usa _RotatingGroqLLM: si una key falla por rate-limit,
cuota agotada o auth inválida, rota automáticamente a la siguiente.
"""

import logging
from langchain_core.language_models import BaseChatModel
from config.settings import settings


# ─── Detección de errores rotables ────────────────────────────────────────────

def _should_rotate(exc: Exception) -> bool:
    """True si el error indica que la key actual está agotada o bloqueada."""
    msg       = str(exc).lower()
    type_name = type(exc).__name__.lower()
    return (
        "429"            in msg
        or "rate"        in msg
        or "quota"       in msg
        or "limit"       in msg
        or "401"         in msg
        or "auth"        in msg
        or "ratelimit"   in type_name
        or "authentication" in type_name
    )


# ─── Wrapper con rotación ─────────────────────────────────────────────────────

class _RotatingGroqLLM:
    """
    Duck-compatible con BaseChatModel (implementa invoke y stream).
    Mantiene N instancias de ChatGroq —  una por key — y rota cuando
    la activa devuelve un error de rate-limit o autenticación.
    """

    def __init__(self, keys: list[str], model: str, temperature: float, **kwargs):
        from langchain_groq import ChatGroq
        self._clients = [
            ChatGroq(model=model, temperature=temperature, api_key=k, **kwargs)
            for k in keys
        ]
        self._current = 0
        logging.info(f"[Groq] {len(keys)} API keys cargadas — rotación automática activa.")

    def _rotate(self):
        self._current = (self._current + 1) % len(self._clients)
        logging.warning(f"[Groq] Rotando a key {self._current + 1}/{len(self._clients)}")

    def invoke(self, input, **kwargs):
        for attempt in range(len(self._clients)):
            try:
                return self._clients[self._current].invoke(input, **kwargs)
            except Exception as e:
                if _should_rotate(e):
                    logging.warning(
                        f"[Groq] Key {self._current + 1} falló "
                        f"({type(e).__name__}) — rotando..."
                    )
                    self._rotate()
                else:
                    raise
        raise RuntimeError(
            f"[Groq] Todas las {len(self._clients)} API keys agotadas o inválidas."
        )

    def stream(self, input, **kwargs):
        """
        Intenta el stream con la key actual. Si falla antes o durante la
        generación, rota y reintenta desde el principio con la key siguiente.
        """
        for attempt in range(len(self._clients)):
            try:
                yield from self._clients[self._current].stream(input, **kwargs)
                return
            except Exception as e:
                if _should_rotate(e):
                    logging.warning(
                        f"[Groq] Key {self._current + 1} falló en stream "
                        f"({type(e).__name__}) — rotando y reintentando..."
                    )
                    self._rotate()
                else:
                    raise
        raise RuntimeError(
            f"[Groq] Todas las {len(self._clients)} API keys agotadas o inválidas."
        )


# ─── Factories públicas ───────────────────────────────────────────────────────

def get_llm():
    """Modelo principal para las respuestas de Iris."""
    provider    = settings.llm.provider
    model       = settings.llm.model
    temperature = settings.llm.temperature

    match provider:
        case "groq":
            keys = settings.llm.api_keys
            if not keys:
                raise ValueError("No hay API keys de Groq configuradas. Define GROQ_API_KEYS en .env")
            if len(keys) == 1:
                from langchain_groq import ChatGroq
                return ChatGroq(model=model, temperature=temperature, api_key=keys[0])
            return _RotatingGroqLLM(keys=keys, model=model, temperature=temperature)

        case "ollama":
            from langchain_ollama import ChatOllama
            return ChatOllama(model=model, temperature=temperature)

        case _:
            raise ValueError(f"Provider '{provider}' no soportado.")


def get_fast_llm(temperature: float = 0.8):
    """
    El modelo rápido, pero escribiendo prosa.

    Es el mismo de get_analysis_llm() sin `response_format: json_object`. Esa
    opción no es un detalle: obliga a que la palabra "json" aparezca en el
    prompt, así que pedirle a ese cliente un texto normal devuelve un 400 —
    exactamente lo que le pasaba al diario.

    Y la temperatura no es 0.0 a propósito. El análisis quiere ser determinista;
    una reflexión escrita a temperatura cero sale plana, y una entrada de diario
    plana no vale para lo que el diario existe.
    """
    keys = settings.llm.api_keys
    if not keys:
        raise ValueError("No hay API keys de Groq configuradas. Define GROQ_API_KEYS en .env")

    model = settings.llm.analysis_model
    if len(keys) == 1:
        from langchain_groq import ChatGroq
        return ChatGroq(model=model, temperature=temperature, api_key=keys[0])
    return _RotatingGroqLLM(keys=keys, model=model, temperature=temperature)


def get_json_llm(model: str):
    """
    Cliente que devuelve JSON garantizado, sobre el modelo que se le diga.

    `response_format: json_object` obliga a que la salida sea JSON válido — y a
    que el prompt contenga la palabra "json", o la API responde 400.
    """
    keys = settings.llm.api_keys
    if not keys:
        raise ValueError("No hay API keys de Groq configuradas. Define GROQ_API_KEYS en .env")

    json_kwargs = {"model_kwargs": {"response_format": {"type": "json_object"}}}
    if len(keys) == 1:
        from langchain_groq import ChatGroq
        return ChatGroq(model=model, temperature=0.0, api_key=keys[0], **json_kwargs)
    return _RotatingGroqLLM(keys=keys, model=model, temperature=0.0, **json_kwargs)


def get_analysis_llm():
    """Modelo rápido para análisis, clasificación y extracción (forzado a JSON)."""
    return get_json_llm(settings.llm.analysis_model)


def get_extraction_llm():
    """
    El modelo GRANDE, forzado a JSON, para sacar conocimiento de la conversación:
    memorias a largo plazo y grafo de entidades.

    La división que sigue el sistema no es "grande para lo importante", sino por
    frecuencia y por si hace falta juicio:

      analysis_llm (20B) — una vez por MENSAJE: humor, intención. Rápido y barato.
      este (120B)        — una vez cada 20 mensajes: qué merece recordarse.

    Y con el pequeño no funcionaba. El grafo llevaba vacío desde que existe
    porque el de 20B no lograba producir JSON válido para un objeto anidado
    —Groq rechazaba la generación entera con `json_validate_failed`— y la
    extracción de memorias, aun cuando no fallaba, guardaba lo que no debía:
    convertía un "abre Spotify" en "prefiere Spotify para escuchar música".

    Decidir qué de una conversación seguirá importando dentro de seis meses es
    un juicio, no una clasificación. Y corriendo una vez cada 20 mensajes, el
    modelo grande aquí no se nota ni en la factura ni en la latencia.
    """
    return get_json_llm(settings.llm.model)
