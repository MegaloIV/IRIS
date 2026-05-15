"""
core/agent.py
Orquestador principal de Iris usando LangGraph.
Soporta streaming del LLM para síntesis de voz inmediata.
"""

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated, Optional, Callable
import operator
import re

from config.settings import settings
from core.personality import PersonalityEngine, Mood
from core.memory import MemoryManager
from core.llm_factory import get_llm, get_analysis_llm
from storage.factory import StorageFactory


class IrisState(TypedDict):
    messages: Annotated[list, operator.add]
    current_mood: str
    trust_level: float
    system_prompt: str
    memory_context: str


class IrisAgent:

    def __init__(self):
        self.storage      = StorageFactory()
        self.llm          = get_llm()
        self.analysis_llm = get_analysis_llm()
        self.stm_window   = settings.memory.stm_window

        self.personality  = PersonalityEngine(state_storage=self.storage.state)
        self.personality.set_analysis_llm(self.analysis_llm)

        self.memory = MemoryManager(
            analysis_llm = self.analysis_llm,
            storage      = self.storage,
        )

        self.conversation_history = self._load_stm_from_db()
        self._voice: Optional[object] = None
        self.graph = self._build_graph()

        stats = self.memory.get_stats()
        print(f"[Iris] Iniciada — {self.personality.get_status_summary()}")
        print(f"[Iris] Memoria: {stats['total_memories']} hechos | {stats['total_messages']} mensajes | STM: {len(self.conversation_history)} cargados")

    def _load_stm_from_db(self) -> list:
        history = []
        rows    = self.memory.load_recent_history()
        for row in rows:
            if row["role"] == "user":
                history.append(HumanMessage(content=row["content"]))
            elif row["role"] == "iris":
                history.append(AIMessage(content=row["content"]))
        return history

    def _build_graph(self):
        workflow = StateGraph(IrisState)
        workflow.add_node("analyze_input",     self._analyze_input_node)
        workflow.add_node("retrieve_memory",   self._retrieve_memory_node)
        workflow.add_node("generate_response", self._generate_response_node)
        workflow.add_node("update_state",      self._update_state_node)
        workflow.set_entry_point("analyze_input")
        workflow.add_edge("analyze_input",     "retrieve_memory")
        workflow.add_edge("retrieve_memory",   "generate_response")
        workflow.add_edge("generate_response", "update_state")
        workflow.add_edge("update_state",      END)
        return workflow.compile()

    # ─── Nodos ────────────────────────────────────────────────────────────────

    def _analyze_input_node(self, state: IrisState) -> dict:
        text    = state["messages"][-1].content
        changes = self.personality.analyze_input(text)
        self.personality.apply_analysis(changes)
        return {
            "messages":       [],
            "current_mood":   self.personality.state.mood.value,
            "trust_level":    self.personality.state.trust_level,
            "system_prompt":  self.personality.build_system_prompt(),
            "memory_context": "",
        }

    def _retrieve_memory_node(self, state: IrisState) -> dict:
        text           = state["messages"][-1].content
        memory_context = self.memory.get_relevant_memories(text)
        return {
            "messages":       [],
            "current_mood":   state["current_mood"],
            "trust_level":    state["trust_level"],
            "system_prompt":  state["system_prompt"],
            "memory_context": memory_context,
        }

    def _generate_response_node(self, state: IrisState) -> dict:
        system_content = state["system_prompt"]
        if state["memory_context"]:
            system_content += "\n\n" + state["memory_context"]

        messages = [SystemMessage(content=system_content)]
        messages.extend(self.conversation_history[-self.stm_window:])
        messages.append(state["messages"][-1])

        response = self.llm.invoke(messages)
        return {
            "messages":       [response],
            "current_mood":   state["current_mood"],
            "trust_level":    state["trust_level"],
            "system_prompt":  state["system_prompt"],
            "memory_context": state["memory_context"],
        }

    def _update_state_node(self, state: IrisState) -> dict:
        user_msg = state["messages"][-2] if len(state["messages"]) >= 2 else state["messages"][-1]
        ai_msg   = state["messages"][-1]

        if hasattr(user_msg, "content") and hasattr(ai_msg, "content"):
            self.conversation_history.append(user_msg)
            self.conversation_history.append(ai_msg)
            if len(self.conversation_history) > self.stm_window * 2:
                self.conversation_history = self.conversation_history[-self.stm_window * 2:]
            self.memory.add_to_session("user", user_msg.content)
            self.memory.add_to_session("iris", ai_msg.content)

        self.personality.save_state()
        return state

    # ─── Interfaz pública ─────────────────────────────────────────────────────

    def chat(self, user_input: str) -> str:
        """Chat normal — retorna texto completo."""
        self.personality.record_interaction()
        initial_state: IrisState = {
            "messages":       [HumanMessage(content=user_input)],
            "current_mood":   self.personality.state.mood.value,
            "trust_level":    self.personality.state.trust_level,
            "system_prompt":  self.personality.build_system_prompt(),
            "memory_context": "",
        }
        result      = self.graph.invoke(initial_state)
        ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
        return ai_messages[-1].content if ai_messages else "..."

    def chat_stream_voice(self, user_input: str, on_sentence: Callable[[str], None]) -> str:
        """
        Chat con streaming para voz.
        Llama on_sentence() con cada oración completa en cuanto el LLM la genera.
        Retorna el texto completo al final.

        Flujo:
          LLM genera "Funcional." → on_sentence("Funcional.") → TTS empieza
          LLM genera " No hay..." → on_sentence("No hay...") → TTS en paralelo
        """
        self.personality.record_interaction()

        # Preparar contexto
        changes = self.personality.analyze_input(user_input)
        self.personality.apply_analysis(changes)

        from config.prompts import VOICE_MODE_ADDON
        system_content = self.personality.build_system_prompt() + "\n" + VOICE_MODE_ADDON
        memory_context = self.memory.get_relevant_memories(user_input)
        if memory_context:
            system_content += "\n\n" + memory_context

        messages = [SystemMessage(content=system_content)]
        messages.extend(self.conversation_history[-self.stm_window:])
        messages.append(HumanMessage(content=user_input))

        # Streaming del LLM
        full_response = ""
        buffer        = ""

        for chunk in self.llm.stream(messages):
            token = chunk.content if hasattr(chunk, "content") else str(chunk)
            full_response += token
            buffer        += token

            # Detectar oración completa
            sentences = re.split(r'(?<=[.!?])\s+', buffer)
            if len(sentences) > 1:
                # Tenemos al menos una oración completa
                for sentence in sentences[:-1]:
                    sentence = sentence.strip()
                    if sentence:
                        on_sentence(sentence)
                buffer = sentences[-1]  # resto sin procesar

        # Enviar lo que quede en el buffer
        if buffer.strip():
            on_sentence(buffer.strip())

        # Guardar en historial
        user_msg = HumanMessage(content=user_input)
        ai_msg   = AIMessage(content=full_response)
        self.conversation_history.append(user_msg)
        self.conversation_history.append(ai_msg)
        if len(self.conversation_history) > self.stm_window * 2:
            self.conversation_history = self.conversation_history[-self.stm_window * 2:]
        self.memory.add_to_session("user", user_input)
        self.memory.add_to_session("iris", full_response)
        self.personality.save_state()

        return full_response

    # ─── Voz ──────────────────────────────────────────────────────────────────

    def start_voice(self, on_speaking_sentence=None, on_listening_changed=None):
        from voice.listener import VoiceListener
        self._voice = VoiceListener(
            on_text_input=self.chat_stream_voice,
            on_speaking_sentence=on_speaking_sentence,
            on_listening_changed=on_listening_changed,
        )
        self._voice.start()
        print("[Iris] Sistema de voz activo.")

    def stop_voice(self):
        if self._voice:
            self._voice.stop()

    def set_tts_enabled(self, enabled: bool):
        if self._voice:
            self._voice.tts_enabled = enabled

    def speak(self, text: str):
        if self._voice:
            self._voice.speak(text)

    # ─── Utils ────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        stats = self.memory.get_stats()
        return {
            "mood":             self.personality.state.mood.value,
            "trust_level":      self.personality.state.trust_level,
            "trust_stage":      self.personality.get_trust_stage().value,
            "energy":           self.personality.state.energy,
            "owner_address":    self.personality.get_owner_address(),
            "total_memories":   stats["total_memories"],
            "total_messages":   stats["total_messages"],
            "session_messages": stats["session_messages"],
            "stm_loaded":       len(self.conversation_history),
            "voice_active":     self._voice is not None,
        }

    def shutdown(self):
        self.stop_voice()
        self.memory.force_close_session()
        self.personality.save_state()
        self.storage.close()

    def reset_conversation(self):
        self.conversation_history = []
        print("[Iris] Conversación reiniciada (memoria y trust intactos)")