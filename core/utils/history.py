from langchain_core.messages import HumanMessage, AIMessage


class ConversationHistory:
    """Wraps the in-memory STM list and keeps it in sync with the memory session."""

    def __init__(self, memory, window: int):
        self._messages: list = []
        self._memory = memory
        self._window = window

    def load(self, rows: list) -> None:
        for row in rows:
            if row["role"] == "user":
                self._messages.append(HumanMessage(content=row["content"]))
            elif row["role"] == "iris":
                self._messages.append(AIMessage(content=row["content"]))

    def append_turn(self, user_content: str, ai_content: str) -> None:
        self._messages.extend([
            HumanMessage(content=user_content),
            AIMessage(content=ai_content),
        ])
        if len(self._messages) > self._window * 2:
            self._messages = self._messages[-self._window * 2:]
        self._memory.add_to_session("user", user_content)
        self._memory.add_to_session("iris", ai_content)

    def get_window(self) -> list:
        return self._messages[-self._window:]

    def reset(self) -> None:
        self._messages = []

    def __len__(self) -> int:
        return len(self._messages)
