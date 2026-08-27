"""
core/memory.py
Memoria de Iris — STM + LTM vectorial y LTM grafo, ambas sobre Postgres.
Extrae memorias cada 30 mensajes usando un agente que decide si es relevante.
"""

import json
import logging
import re
import threading
import unicodedata
import uuid
from datetime import datetime
from typing import Optional

from config.settings import settings
from config.prompts import (
    MEMORY_EXTRACTION_PROMPT,
    MEMORY_CONTEXT_PROMPT,
    GRAPH_EXTRACTION_PROMPT,
    MEMORY_RELEVANCE_PROMPT,
)


def _relative_date(date_str) -> str:
    if not date_str:
        return ""
    try:
        if hasattr(date_str, "isoformat"):
            date = date_str
        else:
            date = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        if hasattr(date, "tzinfo") and date.tzinfo:
            date = date.replace(tzinfo=None)
        delta = datetime.now() - date
        if delta.days == 0:       return "hoy"
        elif delta.days == 1:     return "ayer"
        elif delta.days < 7:      return f"hace {delta.days} días"
        elif delta.days < 30:
            w = delta.days // 7
            return f"hace {w} semana{'s' if w > 1 else ''}"
        elif delta.days < 365:
            m = delta.days // 30
            return f"hace {m} mes{'es' if m > 1 else ''}"
        else:
            y = delta.days // 365
            return f"hace {y} año{'s' if y > 1 else ''}"
    except Exception:
        return ""


def _sanitize_relation(relation: str) -> str:
    normalized = unicodedata.normalize("NFD", relation)
    sanitized  = "".join(c for c in normalized if unicodedata.category(c) != "Mn")
    return sanitized.upper().replace(" ", "_")


def _strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn")


class _EntityMatcher:
    """
    Encuentra qué entidades conocidas aparecen en un texto.

    Sustituye a la llamada a LLM que hacía _extract_query_entities. En vez de
    pedirle al modelo que extraiga nombres de un mensaje, comprueba cuáles de
    los nombres que Iris ya conoce están escritos en él — que es la misma
    pregunta hecha al revés, y sale gratis.

    Además quita un fallo silencioso: cuando el LLM se atragantaba, la función
    antigua devolvía listas vacías y el grafo no aportaba nada sin avisar.

    El matching es sobre texto normalizado (sin acentos, en minúsculas) y con
    límites de palabra, para que "Ana" no case dentro de "mañana".
    """

    TTL_SECONDS = 600     # relee la lista de nombres cada 10 min
    MIN_LEN     = 3       # nombres más cortos generan demasiados falsos positivos
    MAX_MATCHES = 5       # no inundar el traversal

    def __init__(self, graph):
        self._graph     = graph
        self._patterns: list[tuple[re.Pattern, str]] = []
        self._loaded_at = 0.0
        self._lock      = threading.Lock()

    def invalidate(self) -> None:
        """Llamar tras escribir entidades nuevas."""
        with self._lock:
            self._loaded_at = 0.0

    def _refresh_if_stale(self) -> None:
        import time
        if time.monotonic() - self._loaded_at < self.TTL_SECONDS and self._patterns:
            return

        with self._lock:
            if time.monotonic() - self._loaded_at < self.TTL_SECONDS and self._patterns:
                return
            try:
                names = self._graph.get_entity_names()
            except Exception as e:
                logging.warning(f"[Memory] No pude leer nombres de entidad: {e}")
                names = []

            patterns = []
            for name in names:
                norm = _strip_accents(name).lower().strip()
                if len(norm) < self.MIN_LEN:
                    continue
                # Lookarounds en vez de \b: así "C++" o "Node.js" casan bien
                patterns.append(
                    (re.compile(rf"(?<!\w){re.escape(norm)}(?!\w)"), name)
                )

            # Los nombres largos primero: "proyecto Halcón" gana a "Halcón"
            patterns.sort(key=lambda p: len(p[1]), reverse=True)

            self._patterns  = patterns
            self._loaded_at = time.monotonic()
            logging.info(f"[Memory] Matcher de entidades: {len(patterns)} nombres cargados.")

    def find(self, text: str) -> list[str]:
        self._refresh_if_stale()
        if not self._patterns:
            return []

        haystack = _strip_accents(text).lower()
        found: list[str] = []
        for pattern, original in self._patterns:
            if pattern.search(haystack):
                found.append(original)
                if len(found) >= self.MAX_MATCHES:
                    break
        return found


class MemoryManager:

    EXTRACT_EVERY = 20  # mensajes entre extracciones

    def __init__(self, analysis_llm, storage, preferences=None):
        self.analysis_llm = analysis_llm
        self.storage      = storage
        self.preferences  = preferences   # PreferenceEngine, opcional
        self.owner_name   = settings.iris.owner_name
        self.timeout_mins = settings.memory.session_timeout_minutes
        self.stm_persist  = settings.memory.stm_persist_messages

        self._lock = threading.Lock()
        self._session_buffer: list[dict] = []
        self._session_timer: Optional[threading.Timer] = None
        self._message_count = 0  # contador para extracción cada N mensajes

        self._entity_matcher = _EntityMatcher(self.storage.graph)

        logging.info(f"[Memory] Iniciada — {self.storage.vector.count()} memorias vectoriales")

    # ─── Sesión activa ────────────────────────────────────────────────────────

    def add_to_session(self, role: str, content: str):
        msg = {"role": role, "content": content, "timestamp": datetime.now().isoformat()}
        self.storage.history.save_message(role, content)

        with self._lock:
            self._session_buffer.append(msg)
            self._message_count += 1
            should_extract = self._message_count >= self.EXTRACT_EVERY
            if should_extract:
                self._message_count = 0
                buffer = self._session_buffer.copy()

        # Cada 20 mensajes, el agente decide si hay algo relevante
        if should_extract:
            threading.Thread(
                target=self._check_and_extract,
                args=(buffer,),
                daemon=True,
            ).start()

        # Timer de sesión como respaldo
        self._reset_session_timer()

    def _reset_session_timer(self):
        with self._lock:
            if self._session_timer:
                self._session_timer.cancel()
            timer = threading.Timer(self.timeout_mins * 60, self._close_session)
            timer.daemon = True
            timer.start()
            self._session_timer = timer

    def _close_session(self):
        with self._lock:
            if not self._session_buffer:
                return
            logging.info("[Memory] Sesión cerrada por inactividad — extrayendo memorias...")
            buffer = self._session_buffer.copy()
            self._session_buffer = []
            self._session_timer  = None
            self._message_count  = 0
        self._extract_and_store(buffer)

    def force_close_session(self):
        if self._session_timer:
            self._session_timer.cancel()
        self._close_session()

    # ─── Agente de relevancia ─────────────────────────────────────────────────

    def _check_and_extract(self, buffer: list[dict]):
        """
        El agente revisa los últimos N mensajes y decide
        si hay algo relevante que valga guardar.
        """
        conversation = "\n".join(
            f"{msg['role'].capitalize()}: {msg['content']}"
            for msg in buffer
        )

        try:
            prompt   = MEMORY_RELEVANCE_PROMPT.format(
                owner_name   = self.owner_name,
                conversation = conversation,
            )
            response = self.analysis_llm.invoke(prompt)
            content  = re.sub(r'```(?:json)?\s*', '', response.content).strip()
            result   = json.loads(content)

            if result.get("relevant", False):
                logging.info("[Memory] Agente detectó contenido relevante — extrayendo...")
                self._extract_and_store(buffer)
            else:
                logging.info(f"[Memory] Agente: sin relevancia suficiente ({result.get('reason', '')})")

        except Exception as e:
            logging.error(f"[Memory] Error en agente de relevancia: {e}")
            # Si falla el agente, extraer de todas formas por seguridad
            self._extract_and_store(buffer)

    # ─── Extracción ───────────────────────────────────────────────────────────

    def _extract_and_store(self, buffer: list[dict]):
        if not buffer:
            return

        conversation = "\n".join(
            f"{msg['role'].capitalize()}: {msg['content']}"
            for msg in buffer
        )
        current_date = datetime.now().strftime("%Y-%m-%d")

        # Extracción vectorial
        try:
            prompt   = MEMORY_EXTRACTION_PROMPT.format(
                owner_name   = self.owner_name,
                conversation = conversation,
                current_date = current_date,
            )
            response = self.analysis_llm.invoke(prompt)
            content  = re.sub(r'```(?:json)?\s*', '', response.content).strip()
            payload  = json.loads(content)
            facts    = payload.get("facts", [])

            # Del mismo JSON sale lo que Iris sintió — sin llamada extra
            if self.preferences is not None:
                try:
                    n = self.preferences.reinforce_many(payload.get("iris_reactions", []))
                    if n:
                        logging.info(f"[Memory] {n} reacciones de Iris registradas.")
                    self.preferences.prune()
                except Exception as e:
                    logging.warning(f"[Memory] Error registrando reacciones: {e}")

            for fact in facts:
                self.storage.vector.add(
                    memory_id = str(uuid.uuid4()),
                    content   = fact["content"],
                    metadata  = {
                        "category":     fact.get("category", "personal"),
                        "importance":   fact.get("importance", 1),
                        "temporal_ref": fact.get("temporal_ref", ""),
                        "stored_at":    current_date,
                        "owner":        self.owner_name,
                    },
                )
            logging.info(f"[Memory] {len(facts)} memorias vectoriales guardadas.")
        except Exception as e:
            logging.error(f"[Memory] Error extracción vectorial: {e}")

        # Extracción de grafo
        try:
            prompt   = GRAPH_EXTRACTION_PROMPT.format(
                owner_name   = self.owner_name,
                conversation = conversation,
                current_date = current_date,
            )
            response = self.analysis_llm.invoke(prompt)
            content  = re.sub(r'```(?:json)?\s*', '', response.content).strip()
            graph    = json.loads(content)

            for entity in graph.get("entities", []):
                self.storage.graph.add_entity(
                    name        = entity["name"],
                    entity_type = entity.get("type", "Unknown"),
                    properties  = entity.get("properties", {}),
                )

            for rel in graph.get("relations", []):
                self.storage.graph.add_relation(
                    from_name = rel["from"],
                    relation  = _sanitize_relation(rel["relation"]),
                    to_name   = rel["to"],
                    properties = rel.get("properties", {}),
                )

            # Hay entidades nuevas: que el matcher las relea en la próxima consulta
            self._entity_matcher.invalidate()

            logging.info("[Memory] Grafo actualizado.")
        except Exception as e:
            logging.error(f"[Memory] Error extracción grafo: {e}")

    # ─── Recuperación ─────────────────────────────────────────────────────────

    def _extract_query_entities(self, text: str) -> dict:
        """
        Qué entidades conocidas aparecen en el texto.

        Antes esto era una llamada a LLM por cada mensaje. Ahora es una búsqueda
        en memoria contra los nombres que Iris ya tiene guardados — el modelo no
        aportaba nada que no se pueda resolver mirando qué nombres están escritos.

        Los tipos de relación ya no se infieren: el traversal devuelve todas las
        relaciones de las entidades encontradas y es el LLM que lee el contexto
        quien decide cuáles importan, que es donde mejor se le da.
        """
        return {
            "entities":       self._entity_matcher.find(text),
            "relation_types": [],
        }

    def get_relevant_memories(self, query: str, n_results: int = 5) -> str:
        vector_block = self._get_vector_context(query, n_results)

        # Cross-referencing: la extracción de entidades del grafo usa tanto la
        # query del usuario como los hechos que ya trajo la búsqueda vectorial.
        # Así si el vector devuelve "le gusta Python" y el usuario pregunta
        # "¿cómo va?", el grafo puede recuperar relaciones sobre Python.
        enriched_query = f"{query}\n{vector_block}" if vector_block else query
        graph_block    = self._get_graph_context(enriched_query)

        if not vector_block and not graph_block:
            return ""

        parts = []
        if vector_block:
            parts.append(vector_block)
        if graph_block:
            parts.append(f"Relaciones relevantes:\n{graph_block}")

        return MEMORY_CONTEXT_PROMPT.format(
            owner_name = self.owner_name,
            memories   = "\n\n".join(parts),
        )

    def _get_vector_context(self, query: str, n_results: int) -> str:
        if self.storage.vector.count() == 0:
            return ""
        try:
            results = self.storage.vector.query(query, n_results)
            if not results:
                return ""
            results.sort(key=lambda x: x.get("importance", 1), reverse=True)
            lines = []
            for r in results:
                category     = r.get("category", "")
                temporal_ref = r.get("temporal_ref", "")
                stored_at    = r.get("stored_at", "")
                date_label   = _relative_date(temporal_ref or stored_at)
                date_str     = f" ({date_label})" if date_label else ""
                lines.append(f"- [{category}] {r['content']}{date_str}")
            return "\n".join(lines)
        except Exception as e:
            logging.error(f"[Memory] Error recuperando vectores: {e}")
            return ""

    def _get_graph_context(self, query: str) -> str:
        try:
            extracted      = self._extract_query_entities(query)
            entities       = extracted["entities"]
            relation_types = extracted["relation_types"]

            if self.owner_name and self.owner_name not in entities:
                entities = [self.owner_name] + entities

            return self.storage.graph.get_relevant_context(
                entities       = entities,
                relation_types = relation_types,
                owner_name     = self.owner_name,
            )
        except Exception as e:
            logging.error(f"[Memory] Error recuperando grafo: {e}")
            return ""

    # ─── Utils ────────────────────────────────────────────────────────────────

    def load_recent_history(self) -> list[dict]:
        return self.storage.history.load_recent(self.stm_persist)

    def get_all_memories(self) -> list[dict]:
        memories = self.storage.vector.get_all()
        for m in memories:
            temporal_ref = m.get("temporal_ref", "")
            stored_at    = m.get("stored_at", "")
            m["date_label"] = _relative_date(temporal_ref or stored_at)
        return memories

    def get_stats(self) -> dict:
        return {
            "total_memories":   self.storage.vector.count(),
            "total_messages":   self.storage.history.count(),
            "session_messages": len(self._session_buffer),
            "timeout_minutes":  self.timeout_mins,
            "stm_window":       self.stm_persist,
        }