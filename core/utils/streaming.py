import re
from typing import Callable


class SentenceBuffer:
    """
    Acumula tokens sueltos y entrega frases completas según se cierran.

    Existe como clase, y no como bucle dentro de `stream_sentences`, porque hay
    dos fuentes de tokens con formas muy distintas — el stream del LLM de chat y
    el `stream-json` de Claude Code — y el criterio de dónde acaba una frase
    tiene que ser el mismo en las dos: si no, Iris hablaría con un ritmo por
    canal.
    """

    _SPLIT = re.compile(r'(?<=[.!?])\s+')

    def __init__(self, on_sentence: Callable[[str], None]):
        self._on_sentence = on_sentence
        self._buffer      = ""
        self.text         = ""

    def feed(self, token: str) -> None:
        if not token:
            return
        self.text    += token
        self._buffer += token
        partes = self._SPLIT.split(self._buffer)
        if len(partes) > 1:
            for frase in partes[:-1]:
                if f := frase.strip():
                    self._on_sentence(f)
            self._buffer = partes[-1]

    def flush(self) -> None:
        """Entrega lo que quede sin cerrar. Lo último que dice casi nunca lleva punto."""
        if f := self._buffer.strip():
            self._on_sentence(f)
        self._buffer = ""


def stream_sentences(llm, messages: list, on_sentence: Callable[[str], None]) -> str:
    """Stream LLM tokens, calling on_sentence() for each complete sentence."""
    buf = SentenceBuffer(on_sentence)
    for chunk in llm.stream(messages):
        buf.feed(chunk.content if hasattr(chunk, "content") else str(chunk))
    buf.flush()
    return buf.text
