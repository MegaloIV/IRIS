"""
core/agent.py
Orquestador principal de Iris usando LangGraph.
Soporta streaming del LLM para síntesis de voz inmediata.
"""

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, END
from typing import TypedDict, Annotated, Optional, Callable
import logging
import operator
import re
from pathlib import Path

from config.settings import settings
from core.personality import PersonalityEngine
from core.preferences import PreferenceEngine
from core.journal import JournalKeeper
from core.memory import MemoryManager
from core.llm_factory import get_llm, get_analysis_llm, get_fast_llm
from storage.factory import StorageFactory
from core.utils.history import ConversationHistory
from core.utils.context import build_messages
from core.utils.streaming import stream_sentences


_CAPABILITY_RE = re.compile(
    r'\b(puedes|capaz|habilidades?|capacidades?|funciones?|qué sabes|qué puedes|'
    r'sabes hacer|te gustaría|quisieras|puedo pedirte|sirves para|para qué sirves|'
    r'abilities|capabilities|can you)\b',
    re.IGNORECASE,
)
_CAPABILITIES_PATH = Path(__file__).parent.parent / "config" / "capabilities.md"


def _capabilities_context() -> str:
    """Load capabilities.md and wrap it for injection into the system prompt."""
    try:
        content = _CAPABILITIES_PATH.read_text(encoding="utf-8")
        return "[TUS CAPACIDADES — úsalas solo si el usuario pregunta qué puedes hacer o hablan de tus habilidades]:\n" + content
    except Exception:
        return ""


def _is_capability_question(text: str) -> bool:
    return bool(_CAPABILITY_RE.search(text))


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
        # Mismo modelo rápido que el de análisis, pero sin forzar JSON: lo usa
        # la vida interior, que escribe en prosa.
        self.fast_llm     = get_fast_llm()

        self.personality  = PersonalityEngine(state_storage=self.storage.state)
        self.personality.set_analysis_llm(self.analysis_llm)

        self.preferences = PreferenceEngine(self.storage.preferences)
        self.personality.set_preferences(self.preferences)

        # Lo que piensa cuando no hay nadie delante. El motor autónomo
        # (core/proactive.py) es quien lo pone en marcha.
        self.journal = JournalKeeper(self)

        self.memory = MemoryManager(
            analysis_llm = self.analysis_llm,
            storage      = self.storage,
            preferences  = self.preferences,
        )

        self.history = ConversationHistory(self.memory, settings.memory.stm_window)
        self.history.load(self.memory.load_recent_history())

        self._voice: Optional[object] = None
        # (session_id, snapshot de personalidad) de la última delegación, para
        # reanudar la sesión de Claude mientras siga representando a esta Iris.
        self._claude_session: Optional[tuple] = None
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

    def _memory_for(self, text: str) -> str:
        """
        Recuerdos relevantes, más lo que Iris puede hacer si le están preguntando
        justo eso.

        Está aquí y no repetido en cada canal porque si no se olvida en alguno:
        por voz no se inyectaba, así que preguntarle «¿puedes ver mi pantalla?»
        en alto le daba una respuesta peor que escribirlo.
        """
        memoria = self.memory.get_relevant_memories(text)
        if _is_capability_question(text):
            if caps := _capabilities_context():
                return (memoria + "\n\n" + caps).strip() if memoria else caps
        return memoria

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
        memory_context = self._memory_for(text)
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
            system_prompt = system_prompt + "\n\n" + state["interface_context"]
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

        Si lo que le piden es una tarea, va por la vía de delegación igual que
        el texto. Hasta ahora no lo hacía: por voz Iris no podía leer un archivo
        ni mirar la pantalla, aunque por teclado sí — la misma Iris con dos
        juegos de capacidades según por dónde le hablaras.
        """
        if (delegada := self._voice_delegation(user_input, on_sentence)) is not None:
            return delegada

        self.personality.record_interaction()
        changes = self.personality.analyze_input(user_input)
        self.personality.apply_analysis(changes)

        from config.prompts import VOICE_MODE_ADDON
        system_content = self.personality.build_system_prompt() + "\n" + VOICE_MODE_ADDON
        memory_context = self._memory_for(user_input)

        msgs          = build_messages(system_content, memory_context, self.history.get_window(), HumanMessage(content=user_input))
        full_response = stream_sentences(self.llm, msgs, on_sentence)

        self.history.append_turn(user_input, full_response)
        self.personality.save_state()
        return full_response

    def _voice_delegation(self, user_input: str, on_sentence: Callable[[str], None]) -> Optional[str]:
        """
        Atiende por voz lo que sea una tarea. Devuelve None si no lo era.

        None significa "esto no me toca" y deja seguir al chat normal, que es lo
        que pasa con la inmensa mayoría de las frases.
        """
        from core.claude_delegate import IntentAgent, _build_prompt, _quick_delegate_check
        from core.executor import agent_available, stream_claude
        from core.link import protocol as _link
        from core.utils.streaming import SentenceBuffer
        from config.prompts import VOICE_MODE_ADDON

        if not _quick_delegate_check(user_input, None):
            return None

        intent = IntentAgent(self.analysis_llm).analyze(user_input, None)
        if not intent["should_delegate"]:
            return None

        # El escritorio no se puede transmitir mientras se hace: hay que mirar,
        # planificar y ejecutar antes de que haya nada que contar. Se dice entero.
        if intent.get("task_type") == "desktop_control":
            if not settings.companion.enabled:
                return None
            texto = self._handle_desktop_control(
                user_input, intent, settings.companion.url, VOICE_MODE_ADDON,
            )
            buf = SentenceBuffer(on_sentence)
            buf.feed(texto)
            buf.flush()
            return texto

        if not agent_available(_link.CAP_CLAUDE):
            return None   # sin portátil, que lo explique el chat normal

        self.personality.record_interaction()
        changes = self.personality.analyze_input(user_input)
        self.personality.apply_analysis(changes)

        system_content = self.personality.build_system_prompt() + "\n" + VOICE_MODE_ADDON
        if memoria := self._memory_for(user_input):
            system_content += "\n\n" + memoria

        prompt = _build_prompt(user_input, intent, {"owner_name": self.personality.owner_name}, speak_as_iris=True)
        if historial := self._recent_dialogue():
            prompt = f"{historial}\n\n=== LO QUE TE ACABA DE DECIR ===\n{prompt}"

        # A partir de aquí ya se ha contado la interacción y puede haber empezado
        # a hablar, así que este método responde sí o sí: devolver None dejaría
        # que el chat normal volviera a contarla y a hablar encima.
        buf   = SentenceBuffer(on_sentence)
        fallo = ""
        try:
            claude = stream_claude(
                prompt, intent["file_path"],
                system_prompt=system_content,
                resume_session=self._claude_session_for(),
                on_text=buf.feed,
            )
            if not claude.ok:
                fallo = claude.text
        except _link.LinkError as e:
            logging.warning(f"[Claude] Enlace caído durante la voz: {e}")
            claude, fallo = None, str(e)
        buf.flush()

        if fallo:
            logging.warning(f"[Claude] Falló la tarea por voz: {fallo[:200]}")
            # Si ya dijo algo, se queda con eso — repetirlo en otras palabras
            # suena a tartamudeo. Si no llegó a decir nada, lo explica ahora.
            if buf.text.strip():
                respuesta = buf.text
            else:
                respuesta = self._speak_failure(user_input, system_content, fallo, on_sentence)
            self.history.append_turn(user_input, respuesta)
            self.personality.save_state()
            return respuesta

        self._claude_session = (claude.session_id, self._personality_snapshot())
        # El texto dicho es el del stream; `claude.text` es el mismo, ya completo.
        respuesta = claude.text or buf.text
        self.history.append_turn(user_input, respuesta)
        self.personality.save_state()
        return respuesta

    def _speak_failure(self, user_input: str, system_content: str, detalle: str,
                       on_sentence: Callable[[str], None]) -> str:
        """Cuenta en voz alta que la tarea no salió, con la voz de Iris."""
        from core.utils.streaming import stream_sentences
        system = system_content + (
            f"\n\n[Error técnico al ejecutar la tarea]: {detalle}\n"
            "Dile que no pudiste con ello, con tu estilo y en una o dos frases. "
            "No copies el error literal."
        )
        msgs = [SystemMessage(content=system), *self.history.get_window(), HumanMessage(content=user_input)]
        return stream_sentences(self.llm, msgs, on_sentence)

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

    # ─── Control de escritorio ────────────────────────────────────────────────

    _UI_INTERACTION_RE = re.compile(
        r'\b(click|clic|escrib|type|scroll|seleccion|arrastr|drag|men[uú]|bot[oó]n|button|'
        r'busca en|ingres|rellen|campo|input|formulari)\b',
        re.I,
    )

    def _handle_desktop_control(self, user_input: str, intent: dict, companion_url: str, interface_context: str) -> str:
        from core.claude_delegate import take_desktop_snapshot, execute_desktop_actions
        from core.executor import run_claude, desktop_request
        from config.prompts import (
            DESKTOP_LAUNCH_PROMPT, DESKTOP_CONTROL_PROMPT, DESKTOP_ACTIONS_SCHEMA,
        )
        from langchain_core.messages import SystemMessage, HumanMessage

        task     = intent["claude_prompt"]
        needs_ui = bool(self._UI_INTERACTION_RE.search(task))

        # ── Flujo simple: solo lista de apps, sin screenshot ──────────────────
        if not needs_ui:
            try:
                apps = desktop_request("GET", "/apps", timeout=10).get("apps", [])
            except Exception:
                apps = []
            apps_text = "\n".join(f"  - {a}" for a in apps) or "  (lista no disponible)"
            prompt    = DESKTOP_LAUNCH_PROMPT.format(apps=apps_text, task=task)
            claude    = run_claude(prompt, json_schema=DESKTOP_ACTIONS_SCHEMA)

        # ── Flujo completo: screenshot + elementos + coords ───────────────────
        else:
            try:
                snap = take_desktop_snapshot(companion_url)
            except Exception as e:
                return self.chat(f"[No pude tomar screenshot: {e}. Díselo.] {user_input}", interface_context=interface_context)

            elements_text = "\n".join(
                f"  - {el['name']} ({el['type']}) @ ({el['x']}, {el['y']})"
                for el in snap.get("elements", [])[:40]
            ) or "  (ninguno detectado)"

            prompt = DESKTOP_CONTROL_PROMPT.format(
                width=snap.get("width", "?"), height=snap.get("height", "?"),
                elements=elements_text, task=task,
            )
            # La ruta, no la imagen: Claude corre en la misma máquina que acaba
            # de guardar la captura, y la abre él con Read.
            claude = run_claude(
                prompt, snap.get("wsl_path"), json_schema=DESKTOP_ACTIONS_SCHEMA,
            )

        # ── Ejecutar acciones ─────────────────────────────────────────────────
        if not claude.ok:
            return self.chat(
                f"[Intentaste mirar la pantalla y no salió: {claude.text}. "
                f"Díselo con tu estilo, sin tecnicismos.] {user_input}",
                interface_context=interface_context,
            )

        try:
            # --json-schema garantiza la forma, así que esto es un json.loads
            # limpio — antes había que quitarle las comillas de markdown a mano.
            actions = claude.as_json().get("actions", [])
            logging.info(f"[Desktop] {len(actions)} acción(es) planificadas")
            result  = execute_desktop_actions(actions, companion_url)
        except Exception as e:
            logging.warning(f"[Desktop] No pude interpretar las acciones: {e}")
            result = claude.text

        # ── Iris narra lo que acaba de hacer ──────────────────────────────────
        # Esta llamada sí se queda: Claude planificó a ciegas y nunca vio el
        # resultado de ejecutarlas. Narrar lo ejecutado necesita mirar el after.
        self.personality.record_interaction()
        self.personality.save_state()
        self.history.append_turn(user_input, result)

        system = self.personality.build_system_prompt()
        if interface_context:
            system = system + "\n\n" + interface_context
        narration_prompt = (
            f"Acabas de controlar el escritorio tú misma y realizaste estas acciones:\n{result}\n\n"
            f"Dile al usuario lo que hiciste, en primera persona, con tu personalidad. "
            f"Eres tú quien lo hizo — 'lo abrí', 'ya está', 'listo'. "
            f"Sin mencionar JSON, herramientas ni sistemas externos."
        )
        msgs = [SystemMessage(content=system), *self.history.get_window(), HumanMessage(content=narration_prompt)]
        return self.llm.invoke(msgs).content

    # ─── Contexto para la delegación ──────────────────────────────────────────

    _DIALOGUE_TURNS = 6

    def _recent_dialogue(self) -> str:
        """
        Los últimos turnos, en texto plano, para que Claude sepa de qué se habla.

        Claude mantiene su propia sesión con --resume, pero entre delegación y
        delegación hay conversación normal que ocurre en Groq y que él no vio.
        Sin esto, un "y ahora bórralo" no tendría antecedente.
        """
        mensajes = self.history.get_window()[-self._DIALOGUE_TURNS * 2:]
        if not mensajes:
            return ""
        lineas = []
        for m in mensajes:
            quien = "Tú (Iris)" if isinstance(m, AIMessage) else self.personality.owner_name or "El usuario"
            texto = (m.content or "").strip().replace("\n", " ")
            if texto:
                lineas.append(f"{quien}: {texto[:300]}")
        if not lineas:
            return ""
        return "=== LO QUE VENÍAIS HABLANDO ===\n" + "\n".join(lineas)

    def _personality_snapshot(self) -> tuple:
        """Lo que, si cambia, hace que reanudar la sesión de Claude sea mentira."""
        return (
            self.personality.state.mood.value,
            self.personality.get_trust_stage().value,
            self.personality.get_energy_stage(),
        )

    def _claude_session_for(self) -> str:
        """
        El id de sesión a reanudar, si reanudarla sigue siendo honesto.

        --append-system-prompt se fija al abrir la sesión, así que una sesión
        reanudada conserva el humor y la energía que Iris tenía entonces. Barato
        mientras no hayan cambiado; falso en cuanto cambian. Cuando cambian, se
        empieza de cero y Claude vuelve a recibir a la Iris de ahora.
        """
        previa = getattr(self, "_claude_session", None)
        if not previa:
            return ""
        session_id, snapshot = previa
        if snapshot != self._personality_snapshot():
            logging.debug("[Claude] La personalidad cambió; sesión nueva.")
            return ""
        return session_id

    # ─── Delegación a Claude Code ─────────────────────────────────────────────

    def delegate_to_claude(self, user_input: str, file_path: str | None = None, on_delegating: Callable | None = None, interface_context: str = "") -> str:
        """
        Flujo completo de delegación a Claude Code con consciencia de Iris.

        1. Pre-filtro heurístico — descarta mensajes conversacionales sin llamar al LLM
        2. IntentAgent — clasifica la intención y genera el prompt técnico
        3. Contexto de Iris — personalidad, memoria y canal, para el system prompt
        4. Claude Code — hace la tarea y la cuenta él mismo con la voz de Iris
        5. Si falla, Iris lo comunica con su personalidad
        """
        import uuid
        from datetime import datetime, timedelta
        from core.claude_delegate import (
            IntentAgent, _build_prompt, _quick_delegate_check,
            ensure_companion_alive,
        )
        from core.executor import agent_available, run_claude
        from core.link import protocol as _link

        # ── 1. Pre-filtro heurístico ──────────────────────────────────────────
        if not _quick_delegate_check(user_input, file_path):
            return self.chat(user_input, interface_context=interface_context)

        # ── 2. IntentAgent ────────────────────────────────────────────────────
        intent = IntentAgent(self.analysis_llm).analyze(user_input, file_path)
        if not intent["should_delegate"]:
            print("[IntentAgent] Delegación cancelada — respondiendo directamente")
            return self.chat(user_input, interface_context=interface_context)

        # ── 2.5 Companion (desktop_control) ──────────────────────────────────
        if intent.get("task_type") == "desktop_control":
            if not settings.companion.enabled:
                return self.chat(
                    f"[El usuario pidió controlar el escritorio pero el companion está desactivado. "
                    f"Díselo con tu estilo.] {user_input}",
                    interface_context=interface_context,
                )
            alive = ensure_companion_alive(settings.companion.url, settings.companion.startup_timeout)
            if not alive:
                return self.chat(
                    f"[El companion no respondió. Díselo con tu estilo.] {user_input}",
                    interface_context=interface_context,
                )
            if on_delegating:
                on_delegating()
            try:
                return self._handle_desktop_control(
                    user_input, intent, settings.companion.url, interface_context
                )
            except Exception as e:
                logging.error(f"[Desktop] Error no manejado: {e}")
                return self.chat(
                    f"[Hubo un error interno controlando el escritorio: {e}. Díselo brevemente.] {user_input}",
                    interface_context=interface_context,
                )

        # En modo servidor, Claude vive en el portátil. Si está apagado no hay
        # error técnico que dar — hay una capacidad que ahora mismo no tiene, y
        # eso lo cuenta ella. Es el mismo trato que ya recibía el escritorio.
        if not agent_available(_link.CAP_CLAUDE):
            return self.chat(
                f"[Ahora mismo no puedes hacer esto: tus manos para archivos y código "
                f"están en el portátil y está apagado. Díselo con tu estilo, sin "
                f"tecnicismos, y ofrécele hacerlo cuando vuelva a estar.] {user_input}",
                interface_context=interface_context,
            )

        if on_delegating:
            on_delegating()

        # ── 3. Contexto de Iris — va en el system prompt de Claude ────────────
        self.personality.record_interaction()
        changes = self.personality.analyze_input(user_input)
        self.personality.apply_analysis(changes)

        base_prompt    = self.personality.build_system_prompt()
        memory_context = self._memory_for(user_input)
        system_content = (base_prompt + "\n\n" + interface_context) if interface_context else base_prompt
        if memory_context:
            system_content += "\n\n" + memory_context

        iris_context = {
            "owner_name":  self.personality.owner_name,
            "mood":        self.personality.state.mood.value,
            "trust_stage": self.personality.get_trust_stage().value,
        }

        # ── 4. Claude Code, hablando ya como Iris ─────────────────────────────
        # Con la personalidad en --append-system-prompt, lo que devuelve Claude
        # ES la respuesta. Antes hacía falta una tercera llamada a Groq para
        # reescribirla en primera persona: un modelo narrando lo que otro hizo.
        prompt = _build_prompt(user_input, intent, iris_context, speak_as_iris=True)
        historial = self._recent_dialogue()
        if historial:
            prompt = f"{historial}\n\n=== LO QUE TE ACABA DE DECIR ===\n{prompt}"

        resume = self._claude_session_for()
        try:
            claude = run_claude(
                prompt, intent["file_path"],
                system_prompt=system_content,
                resume_session=resume,
            )
            if not claude.ok and resume:
                # Una sesión caducada o de otro directorio falla al reanudarse.
                # Se reintenta limpia antes de dar la tarea por perdida.
                logging.info("[Claude] Falló el --resume; reintento sin sesión previa.")
                self._claude_session = None
                claude = run_claude(
                    prompt, intent["file_path"], system_prompt=system_content,
                )
        except _link.LinkError as e:
            # Estaba conectado al comprobarlo y se cayó a media tarea: el portátil
            # se durmió, o tardó más que el timeout. Cubre también AgentUnavailable,
            # que hereda de LinkError.
            logging.warning(f"[Claude] Enlace con el portátil caído: {e}")
            return self.chat(
                f"[Empezaste la tarea y perdiste el acceso al portátil a media faena. "
                f"Díselo con tu estilo, sin tecnicismos.] {user_input}",
                interface_context=interface_context,
            )

        # ── 5. Si falló, que lo cuente ella ───────────────────────────────────
        if not claude.ok:
            system_content += (
                f"\n\n[Error técnico al ejecutar la tarea]: {claude.text}\n"
                "Informa al usuario de que algo salió mal con tu propio estilo — "
                "no copies el error literal. Exprésalo como Iris: directa, sin drama."
            )
            msgs          = [SystemMessage(content=system_content), *self.history.get_window(), HumanMessage(content=user_input)]
            response_text = self.llm.invoke(msgs).content
            self.history.append_turn(user_input, response_text)
            self.personality.save_state()
            return response_text

        # ── 6. La respuesta de Claude ya es la de Iris ────────────────────────
        self._claude_session = (claude.session_id, self._personality_snapshot())
        response_text        = claude.text

        self.history.append_turn(user_input, response_text)
        self.personality.save_state()

        # ── 7. Guardar en memoria vectorial ───────────────────────────────────
        try:
            snippet    = user_input[:80].strip()
            task_type  = intent.get("task_type", "other")
            # Caduca a los siete días: "te creé este archivo" importa esta
            # semana, no en marzo. Sin la caducidad, cada tarea dejaba una fila
            # permanente en la memoria semántica.
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
        # También la sesión de Claude: si no, seguiría recordando la conversación
        # que se acaba de tirar.
        self._claude_session = None
        print("[Iris] Conversación reiniciada (memoria y trust intactos)")
