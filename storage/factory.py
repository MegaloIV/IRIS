"""
storage/factory.py
Inicializa backends de storage con fallback automático.

Prioridad:
1. Supabase (cloud) — historial, estado, vectores, grafo, preferencias y diario
2. SQLite + ChromaDB (local) — fallback automático, sin grafo
"""

import logging

from storage.base import (
    BaseHistoryStorage,
    BaseJournalStorage,
    BasePreferenceStorage,
    BaseStateStorage,
    BaseVectorStorage,
    BaseGraphStorage,
)


class _DummyGraphStorage(BaseGraphStorage):
    """Graph storage vacío — cuando no hay Postgres (modo local con SQLite)."""

    def add_entity(self, name, entity_type, properties): pass
    def add_relation(self, from_name, relation, to_name, properties=None): pass
    def get_context(self, entity_name, depth=2): return []
    def get_relevant_context(self, entities, relation_types, owner_name): return ""
    def get_owner_graph(self, owner_name, depth=1): return ""
    def save(self): pass
    def close(self): pass
    # Helpers internos que el dummy no necesita implementar
    def get_deep_context(self, entity_name, relation_types, depth=2): return []
    def get_context_by_relation(self, entity_name, relation_types): return []
    def search_entities(self, search_term): return []


class StorageFactory:

    def __init__(self):
        self._init_backends()

    def _init_backends(self):
        self._init_vector_and_history()
        self._init_graph()

    def _init_vector_and_history(self):
        """Intenta Supabase, fallback a SQLite + ChromaDB."""
        from config.settings import settings

        db_url = settings.storage.database_url

        if db_url:
            try:
                from storage.supabase import (
                    init_supabase_schema,
                    SupabaseHistoryStorage,
                    SupabaseJournalStorage,
                    SupabasePreferenceStorage,
                    SupabaseStateStorage,
                    SupabaseVectorStorage,
                )
                init_supabase_schema()
                self.history: BaseHistoryStorage         = SupabaseHistoryStorage()
                self.state: BaseStateStorage             = SupabaseStateStorage()
                self.vector: BaseVectorStorage           = SupabaseVectorStorage()
                self.preferences: BasePreferenceStorage  = SupabasePreferenceStorage()
                self.journal: BaseJournalStorage         = SupabaseJournalStorage()
                logging.info("[Storage] Supabase conectado.")
                return
            except Exception as e:
                logging.warning(f"[Storage] Supabase no disponible: {e}")
                logging.warning("[Storage] Usando SQLite + ChromaDB como fallback.")

        # Fallback local
        from storage.sqlite_fallback import (
            SQLiteHistoryStorage,
            SQLiteJournalStorage,
            SQLitePreferenceStorage,
            SQLiteStateStorage,
            ChromaVectorStorage,
        )
        self.history     = SQLiteHistoryStorage()
        self.state       = SQLiteStateStorage()
        self.vector      = ChromaVectorStorage()
        self.preferences = SQLitePreferenceStorage()
        self.journal     = SQLiteJournalStorage()
        logging.info("[Storage] SQLite + ChromaDB activos (modo local).")

    def _init_graph(self):
        """El grafo vive en el mismo Postgres; dummy silencioso si no hay conexión."""
        from config.settings import settings

        if settings.storage.database_url:
            try:
                from storage.supabase import PostgresGraphStorage
                self.graph: BaseGraphStorage = PostgresGraphStorage()
                counts = self.graph.counts()
                logging.info(
                    f"[Storage] Grafo: {counts['entities']} entidades, "
                    f"{counts['relations']} relaciones."
                )
                return
            except Exception as e:
                logging.warning(f"[Storage] Grafo no disponible: {e}")

        self.graph = _DummyGraphStorage()
        logging.info("[Storage] Grafo desactivado (sin Postgres).")

    def close(self):
        for backend in (self.graph, self.vector, self.history, self.state,
                        self.preferences, self.journal):
            try:
                if hasattr(backend, "close"):
                    backend.close()
            except Exception:
                pass

        # El pool es de módulo, no de una instancia: se cierra aparte
        try:
            from storage.supabase import close_pool
            close_pool()
        except Exception:
            pass