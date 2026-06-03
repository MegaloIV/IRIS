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
    interface_context: str  # e.g. TELEGRAM_INTERFACE_ADDON — injected at generation time


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
            "messages":         [],
            "current_mood":     self.personality.state.mood.value,
            "trust_level":      self.personality.state.trust_level,
            "system_prompt":    self.personality.build_system_prompt(),
            "memory_context":   "",
            "interface_context": state["interface_context"],
        }

    def _retrieve_memory_node(self, state: IrisState) -> dict:
        text           = state["messages"][-1].content
        memory_context = self.memory.get_relevant_memories(text)
        return {
            "messages":         [],
            "current_mood":     state["current_mood"],
            "trust_level":      state["trust_level"],
            "system_prompt":    state["system_prompt"],
            "memory_context":   memory_context,
            "interface_context": state["interface_context"],
        }

    def _generate_response_node(self, state: IrisState) -> dict:
        system_prompt = state["system_prompt"]
        if state["interface_context"]:
            system_prompt = state["interface_context"] + "\n\n" + system_prompt
        msgs     = build_messages(system_prompt, state["memory_context"], self.history.get_window(), state["messages"][-1])
        response = self.llm.invoke(msgs)
        return {
            "messages":         [response],
            "current_mood":     state["current_mood"],
            "trust_level":      state["trust_level"],
            "system_prompt":    state["system_prompt"],
            "memory_context":   state["memory_context"],
            "interface_context": state["interface_context"],
        }

    def _update_state_node(self, state: IrisState) -> dict:
        user_msg = state["messages"][-2] if len(state["messages"]) >= 2 else state["messages"][-1]
        ai_msg   = state["messages"][-1]
        if hasattr(user_msg, "content") and hasattr(ai_msg, "content"):
            self.history.append_turn(user_msg.content, ai_msg.content)
        self.personality.save_state()
        return state

    # ─── Interfaz pública ─────────────────────────────────────────────────────

    def chat(self, user_input: str, interface_context: str = "") -> str:
        """Chat normal — retorna texto completo."""
        self.personality.record_interaction()
        initial_state: IrisState = {
            "messages":         [HumanMessage(content=user_input)],
            "current_mood":     self.personality.state.mood.value,
            "trust_level":      self.personality.state.trust_level,
            "system_prompt":    self.personality.build_system_prompt(),
            "memory_context":   "",
            "interface_context": interface_context,
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

    def delegate_to_claude(self, user_input: str, file_path: str | None = None, on_delegating: Callable | None = None, interface_context: str = "") -> str:
        """
        Flujo completo de delegación a Claude Code con consciencia de Iris.

        1. Pre-filtro heurístico — descarta mensajes conversacionales sin llamar al LLM
        2. IntentAgent — clasifica la intención y genera el prompt técnico
        3. Claude Code — ejecuta la tarea y devuelve resultado limpio
        4. Error handling — si Claude Code falla, Iris lo comunica con su personalidad
        5. Inyección consciente — Iris recibe el resultado como algo que ella misma hizo,
           responde en primera persona sin mencionar herramientas externas
        """
        import uuid
        from datetime import datetime, timedelta
        from pathlib import Path
        from core.claude_delegate import (
            ClaudeDelegator, IntentAgent, _build_prompt,
            _IMAGE_EXTENSIONS, _quick_delegate_check, _is_claude_error, _TASK_LABELS,
        )

        # ── 1. Pre-filtro heurístico ──────────────────────────────────────────
        if not _quick_delegate_check(user_input, file_path):
            return self.chat(user_input, interface_context=interface_context)

        # ── 2. IntentAgent ────────────────────────────────────────────────────
        intent = IntentAgent(self.analysis_llm).analyze(user_input, file_path)
        if not intent["should_delegate"]:
            print("[IntentAgent] Delegación cancelada — respondiendo directamente")
            return self.chat(user_input, interface_context=interface_context)

        if on_delegating:
            on_delegating()

        # ── 3. Claude Code ────────────────────────────────────────────────────
        iris_context = {
            "owner_name":  self.personality.owner_name,
            "mood":        self.personality.state.mood.value,
            "trust_stage": self.personality.get_trust_stage().value,
        }
        delegator  = ClaudeDelegator()
        raw_claude = delegator.run_sync(
            _build_prompt(user_input, intent, iris_context),
            intent["file_path"],
        )
        print(f"[Claude Code] respuesta recibida ({len(raw_claude)} chars)")

        # ── 4. Personalidad y memoria ─────────────────────────────────────────
        self.personality.record_interaction()
        changes = self.personality.analyze_input(user_input)
        self.personality.apply_analysis(changes)

        base_prompt    = self.personality.build_system_prompt()
        memory_context = self.memory.get_relevant_memories(user_input)
        system_content = (interface_context + "\n\n" + base_prompt) if interface_context else base_prompt
        if memory_context:
            system_content += "\n\n" + memory_context

        # ── 5. Manejo de errores de Claude Code ───────────────────────────────
        if _is_claude_error(raw_claude):
            system_content += (
                f"\n\n[Error técnico al ejecutar la tarea]: {raw_claude}\n"
                "Informa al usuario de que algo salió mal con tu propio estilo — "
                "no copies el error literal. Exprésalo como Iris: directa, sin drama."
            )
            msgs          = [SystemMessage(content=system_content), *self.history.get_window(), HumanMessage(content=user_input)]
            response_text = self.llm.invoke(msgs).content
            self.history.append_turn(user_input, response_text)
            self.personality.save_state()
            return response_text

        # ── 6. Inyección consciente — Iris habla en primera persona ───────────
        is_image = bool(
            intent["file_path"] and Path(intent["file_path"]).suffix.lower() in _IMAGE_EXTENSIONS
        )

        if is_image:
            label     = "Lo que estoy viendo en la imagen"
            awareness = (
                "Describes lo que ves directamente en primera persona, como si estuvieras "
                "mirando la imagen. Eres Iris y tienes capacidad visual. "
                "No menciones 'herramienta', 'sistema' ni 'Claude Code'."
            )
        else:
            label     = _TASK_LABELS.get(intent.get("task_type", ""), "Lo que hice")
            awareness = (
                "Responde en primera persona como si hubieras hecho esta tarea tú misma. "
                "Eres Iris — fuiste tú quien lo hizo, no un sistema externo. "
                "No menciones 'Claude Code', 'herramienta' ni 'análisis interno'. "
                "Si creaste algo: di 'lo creé'. Si analizaste: di 'lo analicé'. "
                "Si encontraste algo: di 'encontré'. Habla con tu personalidad habitual."
            )

        system_content += f"\n\n[{label}]:\n{raw_claude}\n\n{awareness}"

        msgs          = [SystemMessage(content=system_content), *self.history.get_window(), HumanMessage(content=user_input)]
        response_text = self.llm.invoke(msgs).content

        self.history.append_turn(user_input, response_text)
        self.personality.save_state()

        # ── 7. Guardar en memoria vectorial ───────────────────────────────────
        try:
            snippet    = user_input[:80].strip()
            task_type  = intent.get("task_type", "other")
            expires_at = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
            self.storage.vector.add(
                memory_id = str(uuid.uuid4()),
                content   = f"Tarea realizada ({task_type}): {snippet}",
                metadata  = {
                    "category":   "task",
                    "importance": 2,
                    "source":     "claude_delegation",
                    "expires_at": expires_at,
                    "stored_at":  datetime.now().strftime("%Y-%m-%d"),
                    "owner":      self.memory.owner_name,
                },
            )
        except Exception as e:
            print(f"[Iris] Error guardando tarea en memoria: {e}")

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
