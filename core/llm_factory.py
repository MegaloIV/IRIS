"""
core/llm_factory.py
Crea LLMs según el proveedor configurado en settings.
"""

from langchain_core.language_models import BaseChatModel
from config.settings import settings


def get_llm() -> BaseChatModel:
    """Modelo principal para las respuestas de Iris."""
    provider    = settings.llm.provider
    model       = settings.llm.model
    temperature = settings.llm.temperature
    api_key     = settings.llm.api_key

    match provider:
        case "groq":
            from langchain_groq import ChatGroq
            return ChatGroq(model=model, temperature=temperature, api_key=api_key)
        case "ollama":
            from langchain_ollama import ChatOllama
            return ChatOllama(model=model, temperature=temperature)
#       case "anthropic":
            from langchain_anthropic import ChatAnthropic
            return ChatAnthropic(model=model, temperature=temperature, api_key=api_key)
#       case "openai":
            from langchain_openai import ChatOpenAI
            return ChatOpenAI(model=model, temperature=temperature, api_key=api_key)
        case _:
            raise ValueError(f"Provider '{provider}' no soportado.")


def get_analysis_llm() -> BaseChatModel:
    """Modelo rápido para análisis, traducción y extracción."""
    from langchain_groq import ChatGroq
    return ChatGroq(
        model       = settings.llm.analysis_model,
        temperature = 0.0,
        api_key     = settings.llm.api_key,
    )