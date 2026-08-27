"""
storage/sqlite_fallback.py
Implementaciones locales usando SQLite y ChromaDB.
Se usan cuando Supabase no está disponible.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

import chromadb
from chromadb.utils import embedding_functions

from config.settings import settings
from storage.base import (
    BaseHistoryStorage,
    BaseJournalStorage,
    BasePreferenceStorage,
    BaseStateStorage,
    BaseVectorStorage,
)


# ─── SQLite History ───────────────────────────────────────────────────────────

class SQLiteHistoryStorage(BaseHistoryStorage):

    def __init__(self, db_path: str = "data/iris.db"):
        os.makedirs("data", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                role      TEXT NOT NULL,
                content   TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        self.conn.commit()
        logging.info("[Storage] SQLite historial listo.")

    def save_message(self, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO conversation_history (role, content, timestamp) VALUES (?, ?, ?)",
            (role, content, datetime.now().isoformat())
        )
        self.conn.commit()

    def load_recent(self, n: int) -> list[dict]:
        cursor = self.conn.execute(
            """
            SELECT role, content, timestamp FROM (
                SELECT id, role, content, timestamp
                FROM conversation_history
                ORDER BY id DESC LIMIT ?
            ) sub ORDER BY id ASC
            """,
            (n,)
        )
        return [{"role": r[0], "content": r[1], "timestamp": r[2]} for r in cursor.fetchall()]

    def count(self) -> int:
        cursor = self.conn.execute("SELECT COUNT(*) FROM conversation_history")
        return cursor.fetchone()[0]


# ─── SQLite State ─────────────────────────────────────────────────────────────

class SQLiteStateStorage(BaseStateStorage):

    def __init__(self, db_path: str = "data/iris.db"):
        os.makedirs("data", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS iris_state (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def save(self, data: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO iris_state (key, value, updated_at)
            VALUES ('iris_emotional_state', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (json.dumps(data), datetime.now().isoformat())
        )
        self.conn.commit()

    def load(self) -> Optional[dict]:
        cursor = self.conn.execute(
            "SELECT value FROM iris_state WHERE key = 'iris_emotional_state'"
        )
        row = cursor.fetchone()
        return json.loads(row[0]) if row else None


# ─── SQLite Preferences ───────────────────────────────────────────────────────

def _pref_row(row) -> dict:
    return {
        "subject":         row[0],
        "kind":            row[1],
        "valence":         row[2],
        "strength":        row[3],
        "formed_at":       row[4],
        "last_reinforced": row[5],
        "evidence":        json.loads(row[6]) if row[6] else [],
    }


class SQLitePreferenceStorage(BasePreferenceStorage):

    def __init__(self, db_path: str = "data/iris.db"):
        os.makedirs("data", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS iris_preferences (
                subject         TEXT PRIMARY KEY,
                kind            TEXT NOT NULL DEFAULT 'tema',
                valence         REAL NOT NULL,
                strength        REAL NOT NULL,
                formed_at       TEXT NOT NULL,
                last_reinforced TEXT NOT NULL,
                evidence        TEXT NOT NULL DEFAULT '[]'
            )
        """)
        self.conn.commit()

    _COLS = "subject, kind, valence, strength, formed_at, last_reinforced, evidence"

    def get_all(self) -> list[dict]:
        cursor = self.conn.execute(
            f"SELECT {self._COLS} FROM iris_preferences ORDER BY strength DESC"
        )
        return [_pref_row(r) for r in cursor.fetchall()]

    def get(self, subject: str) -> Optional[dict]:
        cursor = self.conn.execute(
            f"SELECT {self._COLS} FROM iris_preferences WHERE subject = ?", (subject,)
        )
        row = cursor.fetchone()
        return _pref_row(row) if row else None

    def save(self, preference: dict) -> None:
        now = datetime.now().isoformat()
        self.conn.execute(
            """
            INSERT INTO iris_preferences
                (subject, kind, valence, strength, formed_at, last_reinforced, evidence)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject) DO UPDATE SET
                kind            = excluded.kind,
                valence         = excluded.valence,
                strength        = excluded.strength,
                last_reinforced = excluded.last_reinforced,
                evidence        = excluded.evidence
            """,
            (
                preference["subject"],
                preference.get("kind", "tema"),
                float(preference["valence"]),
                float(preference["strength"]),
                preference.get("formed_at") or now,
                now,
                json.dumps(preference.get("evidence", [])),
            ),
        )
        self.conn.commit()

    def delete(self, subject: str) -> None:
        self.conn.execute("DELETE FROM iris_preferences WHERE subject = ?", (subject,))
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM iris_preferences").fetchone()[0]


# ─── SQLite Journal ───────────────────────────────────────────────────────────

def _journal_row(row) -> dict:
    return {
        "id":      row[0],
        "at":      row[1],
        "kind":    row[2],
        "content": row[3],
        "shared":  bool(row[4]),
        "impulse": row[5],
    }


class SQLiteJournalStorage(BaseJournalStorage):

    def __init__(self, db_path: str = "data/iris.db"):
        os.makedirs("data", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS iris_journal (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                at      TEXT NOT NULL,
                kind    TEXT NOT NULL,
                content TEXT NOT NULL,
                shared  INTEGER NOT NULL DEFAULT 0,
                impulse REAL NOT NULL DEFAULT 0.0
            )
        """)
        self.conn.commit()

    _COLS = "id, at, kind, content, shared, impulse"

    def add(self, kind: str, content: str, impulse: float = 0.0) -> int:
        cursor = self.conn.execute(
            "INSERT INTO iris_journal (at, kind, content, impulse) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(), kind, content, float(impulse)),
        )
        self.conn.commit()
        return cursor.lastrowid

    def recent(self, n: int) -> list[dict]:
        cursor = self.conn.execute(
            f"SELECT {self._COLS} FROM iris_journal ORDER BY at DESC LIMIT ?", (n,)
        )
        return [_journal_row(r) for r in cursor.fetchall()]

    def top_unshared(self) -> Optional[dict]:
        cursor = self.conn.execute(
            f"""
            SELECT {self._COLS} FROM iris_journal
            WHERE shared = 0
            ORDER BY impulse DESC, at DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        return _journal_row(row) if row else None

    def mark_shared(self, entry_id: int) -> None:
        self.conn.execute("UPDATE iris_journal SET shared = 1 WHERE id = ?", (entry_id,))
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM iris_journal").fetchone()[0]


# ─── ChromaDB Vector ──────────────────────────────────────────────────────────

class ChromaVectorStorage(BaseVectorStorage):

    def __init__(self, path: str = "data/chromadb"):
        os.makedirs(path, exist_ok=True)
        self.client     = chromadb.PersistentClient(path=path)
        self.ef         = embedding_functions.DefaultEmbeddingFunction()
        self.collection = self.client.get_or_create_collection(
            name             = "iris_memories",
            embedding_function = self.ef,
            metadata         = {"hnsw:space": "cosine"},
        )
        logging.info(f"[Storage] ChromaDB listo — {self.collection.count()} memorias.")

    def add(self, memory_id: str, content: str, metadata: dict) -> None:
        self.collection.add(
            ids       = [memory_id],
            documents = [content],
            metadatas = [metadata],
        )

    def query(self, text: str, n_results: int) -> list[dict]:
        if self.collection.count() == 0:
            return []
        results   = self.collection.query(
            query_texts = [text],
            n_results   = min(n_results, self.collection.count()),
        )
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        return [{"content": doc, **meta} for doc, meta in zip(documents, metadatas)]

    def get_all(self) -> list[dict]:
        if self.collection.count() == 0:
            return []
        results = self.collection.get(include=["documents", "metadatas"])
        return [
            {"content": doc, **meta}
            for doc, meta in zip(results["documents"], results["metadatas"])
        ]

    def count(self) -> int:
        return self.collection.count()