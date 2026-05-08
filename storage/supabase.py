"""
storage/supabase.py
Implementaciones de storage usando Supabase (PostgreSQL + pgvector).
Cubre: historial, estado emocional y vectores.
"""

import json
import logging
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras
from sentence_transformers import SentenceTransformer

from config.settings import settings
from storage.base import BaseHistoryStorage, BaseStateStorage, BaseVectorStorage


def _get_conn():
    return psycopg2.connect(settings.storage.database_url)


def init_supabase_schema():
    """
    Crea las tablas necesarias en Supabase si no existen.
    Ejecutar una sola vez al iniciar.
    """
    conn = _get_conn()
    cur  = conn.cursor()

    # Habilitar pgvector
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    # Historial de conversación
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversation_history (
            id        SERIAL PRIMARY KEY,
            role      TEXT NOT NULL,
            content   TEXT NOT NULL,
            timestamp TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    # Estado emocional de Iris
    cur.execute("""
        CREATE TABLE IF NOT EXISTS iris_state (
            id         SERIAL PRIMARY KEY,
            key        TEXT UNIQUE NOT NULL,
            value      JSONB NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    # Memorias semánticas con embeddings
    cur.execute("""
        CREATE TABLE IF NOT EXISTS iris_memories (
            id           TEXT PRIMARY KEY,
            content      TEXT NOT NULL,
            embedding    vector(384),
            category     TEXT,
            importance   INT DEFAULT 1,
            temporal_ref TEXT,
            stored_at    TIMESTAMPTZ DEFAULT NOW(),
            owner        TEXT
        );
    """)

    # Índice para búsqueda por similitud
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_memories_embedding
        ON iris_memories USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 100);
    """)

    conn.commit()
    cur.close()
    conn.close()
    logging.info("[Supabase] Schema inicializado.")


# ─── History Storage ──────────────────────────────────────────────────────────

class SupabaseHistoryStorage(BaseHistoryStorage):

    def save_message(self, role: str, content: str) -> None:
        conn = _get_conn()
        cur  = conn.cursor()
        cur.execute(
            "INSERT INTO conversation_history (role, content) VALUES (%s, %s)",
            (role, content)
        )
        conn.commit()
        cur.close()
        conn.close()

    def load_recent(self, n: int) -> list[dict]:
        conn = _get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT role, content, timestamp FROM (
                SELECT role, content, timestamp
                FROM conversation_history
                ORDER BY id DESC LIMIT %s
            ) sub ORDER BY timestamp ASC
            """,
            (n,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(r) for r in rows]

    def count(self) -> int:
        conn = _get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM conversation_history")
        n = cur.fetchone()[0]
        cur.close()
        conn.close()
        return n


# ─── State Storage ────────────────────────────────────────────────────────────

class SupabaseStateStorage(BaseStateStorage):

    KEY = "iris_emotional_state"

    def save(self, data: dict) -> None:
        conn = _get_conn()
        cur  = conn.cursor()
        cur.execute(
            """
            INSERT INTO iris_state (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value,
                    updated_at = NOW()
            """,
            (self.KEY, json.dumps(data))
        )
        conn.commit()
        cur.close()
        conn.close()

    def load(self) -> Optional[dict]:
        conn = _get_conn()
        cur  = conn.cursor()
        cur.execute(
            "SELECT value FROM iris_state WHERE key = %s",
            (self.KEY,)
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else None


# ─── Vector Storage ───────────────────────────────────────────────────────────

class SupabaseVectorStorage(BaseVectorStorage):

    def __init__(self):
        # Modelo de embeddings — mismo que ChromaDB usa por defecto
        self.encoder = SentenceTransformer("all-MiniLM-L6-v2")

    def _embed(self, text: str) -> list[float]:
        return self.encoder.encode(text).tolist()

    def add(self, memory_id: str, content: str, metadata: dict) -> None:
        embedding = self._embed(content)
        conn = _get_conn()
        cur  = conn.cursor()
        cur.execute(
            """
            INSERT INTO iris_memories
                (id, content, embedding, category, importance, temporal_ref, owner)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                memory_id,
                content,
                embedding,
                metadata.get("category", "personal"),
                metadata.get("importance", 1),
                metadata.get("temporal_ref", ""),
                metadata.get("owner", ""),
            )
        )
        conn.commit()
        cur.close()
        conn.close()

    def query(self, text: str, n_results: int) -> list[dict]:
        embedding = self._embed(text)
        conn = _get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id, content, category, importance, temporal_ref, stored_at,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM iris_memories
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (embedding, embedding, n_results)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(r) for r in rows]

    def get_all(self) -> list[dict]:
        conn = _get_conn()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, content, category, importance, temporal_ref, stored_at FROM iris_memories ORDER BY importance DESC"
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [dict(r) for r in rows]

    def count(self) -> int:
        conn = _get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM iris_memories")
        n = cur.fetchone()[0]
        cur.close()
        conn.close()
        return n