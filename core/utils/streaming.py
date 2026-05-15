import re
from typing import Callable


def stream_sentences(llm, messages: list, on_sentence: Callable[[str], None]) -> str:
    """Stream LLM tokens, calling on_sentence() for each complete sentence."""
    full_response = ""
    buffer = ""
    for chunk in llm.stream(messages):
        token = chunk.content if hasattr(chunk, "content") else str(chunk)
        full_response += token
        buffer += token
        sentences = re.split(r'(?<=[.!?])\s+', buffer)
        if len(sentences) > 1:
            for sentence in sentences[:-1]:
                if s := sentence.strip():
                    on_sentence(s)
            buffer = sentences[-1]
    if s := buffer.strip():
        on_sentence(s)
    return full_response
