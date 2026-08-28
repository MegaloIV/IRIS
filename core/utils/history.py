import logging
import threading

from langchain_core.messages import HumanMessage, AIMessage


class ConversationHistory:
    """
    Wraps the in-memory STM list and keeps it in sync with the memory session.

    Cinco hilos tocan esto a la vez — la UI, el bucle de terminal, la voz, el
    motor proactivo y el ejecutor de Telegram — así que las mutaciones van bajo
    lock. Sin él, un mensaje de Telegram mientras hablas por voz intercala los
    turnos y el historial deja de tener sentido.

    El lock protege solo la lista; add_to_session() se llama fuera porque hace
    E/S y no queremos retener el lock durante una escritura a la base de datos.
    """

    def __init__(self, memory, window: int):
        self._messages: list = []
        self._memory = memory
        self._window = window
        self._lock = threading.Lock()

    def load(self, rows: list) -> None:
        msgs = []
        for row in rows:
            if row["role"] == "user":
                msgs.append(HumanMessage(content=row["content"]))
            elif row["role"] == "iris":
                msgs.append(AIMessage(content=row["content"]))
        with self._lock:
            self._messages.extend(msgs)

    def append_turn(self, user_content: str, ai_content: str) -> None:
        # Un turno a medias envenena todo lo que venga después: va en la ventana
        # de contexto de cada mensaje siguiente, y un turno de la IA en blanco le
        # enseña al modelo que responder nada es aceptable. Mejor perder el turno
        # que arrastrarlo.
        if not (user_content or "").strip() or not (ai_content or "").strip():
            logging.warning("[Historial] Turno vacío descartado, no se guarda.")
            return

        with self._lock:
            self._messages.extend([
                HumanMessage(content=user_content),
                AIMessage(content=ai_content),
            ])
            if len(self._messages) > self._window * 2:
                self._messages = self._messages[-self._window * 2:]

        self._memory.add_to_session("user", user_content)
        self._memory.add_to_session("iris", ai_content)

    def get_window(self) -> list:
        with self._lock:
            return list(self._messages[-self._window:])

    def reset(self) -> None:
        with self._lock:
            self._messages = []

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)
