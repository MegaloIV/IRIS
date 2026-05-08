"""
storage/factory.py
Inicializa backends de storage con fallback automático.

Prioridad:
1. Supabase + Neo4j (cloud) — si están disponibles
2. SQLite + ChromaDB (local) — fallback automático
"""

import logging

from storage.base import (
    BaseHistoryStorage,
    BaseStateStorage,
    BaseVectorStorage,
    BaseGraphStorage,
)


class _DummyGraphStorage(BaseGraphStorage):
    """Graph storage vacío — cuando Neo4j no está disponible."""

    def add_entity(self, name, entity_type, properties): pass
    def add_relation(self, from_name, relation, to_name): pass
    def get_context(self, entity_name, depth=2): return []
    def get_relevant_context(self, entities, relation_types, owner_name): return ""
    def get_owner_graph(self, owner_name, depth=1): return ""
    def save(self): pass
    def close(self): pass


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
                    SupabaseStateStorage,
                    SupabaseVectorStorage,
                )
                init_supabase_schema()
                self.history: BaseHistoryStorage = SupabaseHistoryStorage()
                self.state: BaseStateStorage     = SupabaseStateStorage()
                self.vector: BaseVectorStorage   = SupabaseVectorStorage()
                logging.info("[Storage] Supabase conectado.")
                return
            except Exception as e:
                logging.warning(f"[Storage] Supabase no disponible: {e}")
                logging.warning("[Storage] Usando SQLite + ChromaDB como fallback.")

        # Fallback local
        from storage.sqlite_fallback import (
            SQLiteHistoryStorage,
            SQLiteStateStorage,
            ChromaVectorStorage,
        )
        self.history = SQLiteHistoryStorage()
        self.state   = SQLiteStateStorage()
        self.vector  = ChromaVectorStorage()
        logging.info("[Storage] SQLite + ChromaDB activos (modo local).")

    def _init_graph(self):
        """Intenta Neo4j, fallback a dummy silencioso."""
        from config.settings import settings

        neo4j_uri = settings.storage.neo4j_uri

        if neo4j_uri:
            try:
                from storage.neo4j import Neo4jGraphStorage
                self.graph: BaseGraphStorage = Neo4jGraphStorage()
                logging.info("[Storage] Neo4j conectado.")
                return
            except Exception as e:
                logging.warning(f"[Storage] Neo4j no disponible: {e}")

        self.graph = _DummyGraphStorage()
        logging.info("[Storage] Graph storage desactivado (modo local).")

    def close(self):
        try:
            if hasattr(self.graph, "close"):
                self.graph.close()
        except Exception:
            pass