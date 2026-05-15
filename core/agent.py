"""
core/agent.py
Orquestador principal de Iris usando LangGraph.
Soporta streaming del LLM para síntesis de voz inmediata.
"""

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated, Optional, Callable
import operator

from config.settings import settings
from core.personality import PersonalityEngine
from core.memory import MemoryManager
from core.llm_factory import get_llm, get_analysis_llm
from storage.factory import StorageFactory
from core.utils.history import ConversationHistory
from core.utils.context import build_messages
from core.utils.streaming import stream_sentences


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

        self.personality  = PersonalityEngine(state_storage=self.storage.state)
        self.personality.set_analysis_llm(self.analysis_llm)

        self.memory = MemoryManager(
            analysis_llm = self.analysis_llm,
            storage      = self.storage,
        )

        self.history = ConversationHistory(self.memory, settings.memory.stm_window)
        self.history.load(self.memory.load_recent_history())

        self._voice: Optional[object] = None
        self.graph = self._build_graph()

        stats = self.memory.get_stats()
        print(f"[Iris] Iniciada — {self.personality.get_status_summary()}")
        print(f"[Iris] Memoria: {stats['total_memories']} hechos | {stats['total_messages']} mensajes | STM: {len(self.history)} cargados")

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
        msgs     = build_messages(state["system_prompt"], state["memory_context"], self.history.get_window(), state["messages"][-1])
        response = self.llm.invoke(msgs)
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
            self.history.append_turn(user_msg.content, ai_msg.content)
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
        """
        self.personality.record_interaction()
        changes = self.personality.analyze_input(user_input)
        self.personality.apply_analysis(changes)

        from config.prompts import VOICE_MODE_ADDON
        system_content = self.personality.build_system_prompt() + "\n" + VOICE_MODE_ADDON
        memory_context = self.memory.get_relevant_memories(user_input)

        msgs          = build_messages(system_content, memory_context, self.history.get_window(), HumanMessage(content=user_input))
        full_response = stream_sentences(self.llm, msgs, on_sentence)

        self.history.append_turn(user_input, full_response)
        self.personality.save_state()
        return full_response

    # ─── Voz ──────────────────────────────────────────────────────────────────

    def start_voice(self, on_speaking_sentence=None, on_listening_changed=None):
        from voice.listener import VoiceListener
        self._voice = VoiceListener(
            on_text_input        = self.chat_stream_voice,
            on_speaking_sentence = on_speaking_sentence,
            on_listening_changed = on_listening_changed,
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

    # ─── Delegación a Claude Code ─────────────────────────────────────────────

    def delegate_to_claude(self, user_input: str, file_path: str | None = None, on_delegating: Callable | None = None) -> str:
        """
        Runs Claude Code for complex analysis, injects the raw output as
        internal system context, then generates Iris's response through her
        full personality pipeline.  The original user message is what gets
        stored in conversation history — not the injected prompt.

        Flow:
          1. IntentAgent (Groq) — understands real intent, generates optimized prompt
          2. ClaudeDelegator   — calls Claude Code subprocess with that prompt
          3. Iris pipeline     — personality-shapes the response before showing it
        """
        import uuid
        from datetime import datetime, timedelta
        from core.claude_delegate import ClaudeDelegator, IntentAgent, _build_prompt

        intent = IntentAgent(self.analysis_llm).analyze(user_input, file_path)
        if not intent["should_delegate"]:
            print("[IntentAgent] Delegación cancelada — respondiendo directamente")
            return self.chat(user_input)

        if on_delegating:
            on_delegating()

        delegator  = ClaudeDelegator()
        raw_claude = delegator.run_sync(_build_prompt(user_input, intent), intent["file_path"])
        print(f"[Claude Code] respuesta recibida ({len(raw_claude)} chars)")

        self.personality.record_interaction()
        changes = self.personality.analyze_input(user_input)
        self.personality.apply_analysis(changes)

        system_content  = self.personality.build_system_prompt()
        memory_context  = self.memory.get_relevant_memories(user_input)
        if memory_context:
            system_content += "\n\n" + memory_context
        system_content += (
            "\n\n[Análisis interno — procesado por Claude Code]\n"
            f"{raw_claude}\n"
            "[Fin del análisis interno]\n"
            "Usa este análisis como base. Responde como Iris, con tu personalidad actual."
        )

        msgs          = [SystemMessage(content=system_content), *self.history.get_window(), HumanMessage(content=user_input)]
        response_text = self.llm.invoke(msgs).content

        self.history.append_turn(user_input, response_text)
        self.personality.save_state()

        try:
            snippet    = user_input[:80].strip()
            expires_at = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
            summary    = (
                f"Delegación a Claude Code: el usuario pidió '{snippet}'. "
                f"Análisis completado y respondido por Iris."
            )
            self.storage.vector.add(
                memory_id = str(uuid.uuid4()),
                content   = summary,
                metadata  = {
                    "category":   "delegation",
                    "importance": 1,
                    "source":     "claude_delegation",
                    "expires_at": expires_at,
                    "stored_at":  datetime.now().strftime("%Y-%m-%d"),
                    "owner":      self.memory.owner_name,
                },
            )
        except Exception as e:
            print(f"[Iris] Error guardando resumen de delegación: {e}")

        return response_text

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
            "stm_loaded":       len(self.history),
            "voice_active":     self._voice is not None,
        }

    def shutdown(self):
        self.stop_voice()
        self.memory.force_close_session()
        self.personality.save_state()
        self.storage.close()

    def reset_conversation(self):
        self.history.reset()
        print("[Iris] Conversación reiniciada (memoria y trust intactos)")
