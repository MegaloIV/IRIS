from langchain_core.messages import SystemMessage, HumanMessage


def build_messages(
    system_prompt: str,
    memory_context: str,
    history_window: list,
    user_message: HumanMessage,
) -> list:
    content = system_prompt
    if memory_context:
        content += "\n\n" + memory_context
    return [SystemMessage(content=content), *history_window, user_message]
