"""
storage/supabase.py
Implementaciones de storage usando Supabase (PostgreSQL + pgvector).
Cubre: historial, estado emocional, vectores, grafo, preferencias y diario.

Todas las operaciones pasan por un pool de conexiones. Abrir una conexión nueva
por operación costaba ~2 s contra el pooler de Supabase (handshake TCP + TLS +
auth), y un turno de conversación hace media docena de operaciones.
"""

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from config.settings import settings
from storage.base import (
    BaseGraphStorage,
    BaseHistoryStorage,
    BaseJournalStorage,
    BasePreferenceStorage,
    BaseStateStorage,
    BaseVectorStorage,
)


# ─── Pool de conexiones ───────────────────────────────────────────────────────

_POOL: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_POOL_LOCK = threading.Lock()

# Iris corre cinco hilos (UI, terminal, voz, proactivo, Telegram); 10 sobra.
_POOL_MIN = 1
_POOL_MAX = 10

# Keepalives TCP: evitan que el pooler tire conexiones inactivas y que el pool
# reparta conexiones muertas tras un rato de silencio.
_KEEPALIVE = {
    "keepalives":          1,
    "keepalives_idle":     30,
    "keepalives_interval": 10,
    "keepalives_count":    5,
}


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = psycopg2.pool.ThreadedConnectionPool(
                _POOL_MIN, _POOL_MAX, settings.storage.database_url, **_KEEPALIVE
            )
            logging.info(f"[Supabase] Pool de conexiones listo ({_POOL_MIN}-{_POOL_MAX}).")
    return _POOL


def close_pool() -> None:
    """Cierra todas las conexiones. Lo llama StorageFactory.close()."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            _POOL.closeall()
            _POOL = None
            logging.info("[Supabase] Pool cerrado.")


@contextmanager
def _cursor(commit: bool = False, dict_rows: bool = False):
    """
    Cursor sobre una conexión del pool.

    La conexión vuelve al pool pase lo que pase — si una excepción escapara sin
    devolverla, el pool se agotaría y todo se quedaría bloqueado esperando. Por
    eso el `finally` es lo importante de esta función, no la comodidad.

    commit=True para escrituras; en caso de error hace rollback y relanza.

    Las lecturas van en autocommit a propósito. psycopg2 abre una transacción
    implícita incluso para un SELECT, y entonces putconn() la deshace con un
    rollback — un viaje de red entero de más por cada lectura, que a 200 ms de
    latencia duplicaba el coste de todas las consultas.
    """
    pool = _get_pool()
    conn = pool.getconn()
    try:
        # Una conexión que el servidor cerró por su cuenta se descarta y se pide otra
        if conn.closed:
            pool.putconn(conn, close=True)
            conn = pool.getconn()

        conn.autocommit = not commit

        factory = psycopg2.extras.RealDictCursor if dict_rows else None
        cur = conn.cursor(cursor_factory=factory)
        try:
            yield cur
            if commit:
                conn.commit()
        except Exception:
            if not conn.autocommit:
                conn.rollback()
            raise
        finally:
            cur.close()
    finally:
        pool.putconn(conn)


# ─── Esquema ──────────────────────────────────────────────────────────────────

def init_supabase_schema():
    """Crea las tablas e índices necesarios si no existen. Idempotente."""
    with _cursor(commit=True) as cur:
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
                owner        TEXT,
                expires_at   DATE
            );
        """)

        # Para bases que ya existían: la columna es nueva, la escritura no.
        # `delegate_to_claude` lleva calculando expires_at desde siempre y este
        # storage lo tiraba a la basura sin decir nada, así que los registros de
        # tareas se acumulaban para siempre compitiendo con los recuerdos de
        # verdad por los cinco huecos de contexto de cada turno.
        cur.execute("ALTER TABLE iris_memories ADD COLUMN IF NOT EXISTS expires_at DATE;")

        # Y las que ya estaban guardadas nacieron sin fecha, o sea eternas. Se
        # les pone la que les habría tocado, contando desde el día en que se
        # guardaron: las viejas quedan caducadas y se van en la siguiente purga,
        # las de esta semana aguantan lo que les quede.
        #
        # Solo toca filas de tareas sin fecha, así que la segunda vez no hace
        # nada — las nuevas ya nacen con la suya.
        cur.execute("""
            UPDATE iris_memories
               SET expires_at = (stored_at + INTERVAL '7 days')::date
             WHERE category = 'task' AND expires_at IS NULL
        """)
        if cur.rowcount:
            logging.info(f"[Supabase] {cur.rowcount} registros de tareas antiguos: caducidad asignada.")

        # Índice de similitud: HNSW, no ivfflat.
        #
        # ivfflat agrupa los vectores en `lists` clusters con k-means y luego
        # busca solo en los más cercanos. Ese k-means necesita datos para
        # entrenarse: creado sobre una tabla vacía, los centroides no
        # representan nada y la recuperación sale mal. Y como los centroides no
        # se recalculan al insertar, hay que hacer REINDEX cada cierto tiempo.
        #
        # HNSW construye un grafo navegable de forma incremental: no tiene fase
        # de entrenamiento, así que da igual crearlo en vacío, y se mantiene
        # correcto según entran filas. Cuesta más de construir y ocupa más
        # memoria, lo cual es irrelevante a esta escala.
        cur.execute("DROP INDEX IF EXISTS idx_memories_embedding;")
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
            ON iris_memories USING hnsw (embedding vector_cosine_ops);
        """)

        # Preferencias — gustos y aversiones que Iris se forma sola
        cur.execute("""
            CREATE TABLE IF NOT EXISTS iris_preferences (
                subject         TEXT PRIMARY KEY,
                kind            TEXT NOT NULL DEFAULT 'tema',
                valence         REAL NOT NULL,
                strength        REAL NOT NULL,
                formed_at       TIMESTAMPTZ DEFAULT NOW(),
                last_reinforced TIMESTAMPTZ DEFAULT NOW(),
                evidence        JSONB DEFAULT '[]'
            );
        """)

        # Diario — lo que hace y piensa cuando no hay nadie delante
        cur.execute("""
            CREATE TABLE IF NOT EXISTS iris_journal (
                id      BIGSERIAL PRIMARY KEY,
                at      TIMESTAMPTZ DEFAULT NOW(),
                kind    TEXT NOT NULL,
                content TEXT NOT NULL,
                shared  BOOLEAN DEFAULT FALSE,
                impulse REAL DEFAULT 0.0
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_journal_unshared
            ON iris_journal (impulse DESC, at DESC)
            WHERE shared = FALSE;
        """)

        # Grafo de conocimiento
        cur.execute("""
            CREATE TABLE IF NOT EXISTS iris_entities (
                name       TEXT PRIMARY KEY,
                type       TEXT DEFAULT 'Unknown',
                properties JSONB DEFAULT '{}',
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS iris_relations (
                from_name  TEXT NOT NULL REFERENCES iris_entities(name) ON DELETE CASCADE,
                relation   TEXT NOT NULL,
                to_name    TEXT NOT NULL REFERENCES iris_entities(name) ON DELETE CASCADE,
                context    TEXT DEFAULT '',
                rel_date   TEXT DEFAULT '',
                history    JSONB DEFAULT '[]',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (from_name, relation, to_name)
            );
        """)
        # El traversal recorre aristas en ambos sentidos — un índice por extremo
        cur.execute("CREATE INDEX IF NOT EXISTS idx_relations_from ON iris_relations (from_name);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_relations_to   ON iris_relations (to_name);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_entities_lower ON iris_entities (LOWER(name));")

    logging.info("[Supabase] Schema inicializado.")


# ─── History Storage ──────────────────────────────────────────────────────────

class SupabaseHistoryStorage(BaseHistoryStorage):

    def save_message(self, role: str, content: str) -> None:
        with _cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO conversation_history (role, content) VALUES (%s, %s)",
                (role, content),
            )

    def load_recent(self, n: int) -> list[dict]:
        with _cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT role, content, timestamp FROM (
                    SELECT role, content, timestamp
                    FROM conversation_history
                    ORDER BY id DESC LIMIT %s
                ) sub ORDER BY timestamp ASC
                """,
                (n,),
            )
            return [dict(r) for r in cur.fetchall()]

    def count(self) -> int:
        with _cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM conversation_history")
            return cur.fetchone()[0]


# ─── State Storage ────────────────────────────────────────────────────────────

class SupabaseStateStorage(BaseStateStorage):

    KEY = "iris_emotional_state"

    def save(self, data: dict) -> None:
        with _cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO iris_state (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value,
                        updated_at = NOW()
                """,
                (self.KEY, json.dumps(data)),
            )

    def load(self) -> Optional[dict]:
        with _cursor() as cur:
            cur.execute("SELECT value FROM iris_state WHERE key = %s", (self.KEY,))
            row = cur.fetchone()
            return row[0] if row else None


# ─── Vector Storage ───────────────────────────────────────────────────────────

_EMBED_MODEL = "all-MiniLM-L6-v2"

# Por encima de esto, dos memorias son la misma. El umbral es alto a propósito, y
# no por prudencia genérica — medido con este mismo encoder:
#
#   frase idéntica repetida .............. 1.000
#   "le gusta el azul" / "...el verde" ... 0.934   ← hechos DISTINTOS
#   "abre Crunchyroll" / "abre Discord" .. 0.845   ← hechos DISTINTOS
#   "a Matt le gusta X" / "a Matías le gusta mucho X" ... 0.782  ← el MISMO
#   el mismo hecho reformulado ........... 0.750
#
# Es decir: dos valores contradictorios del mismo atributo se parecen MÁS que una
# paráfrasis real. Así que no existe ningún umbral que pille las paráfrasis sin
# fusionar «azul» con «verde», y perder un hecho de verdad es mucho peor que
# guardar un duplicado. Esto solo caza repeticiones casi literales — que es
# justo lo que se estaba colando, porque ON CONFLICT (id) nunca casaba: el id
# es un uuid nuevo en cada inserción.
_DUPLICADO_SOBRE = 0.97


class SupabaseVectorStorage(BaseVectorStorage):

    def __init__(self):
        from sentence_transformers import SentenceTransformer

        # Con el modelo ya cacheado, SentenceTransformer hace ~25 peticiones a
        # HuggingFace solo para comprobar si hay versión nueva. En un servidor
        # eso es latencia de arranque y una dependencia externa innecesaria, así
        # que se intenta primero en local y solo se sale a la red si no está.
        try:
            self.encoder = SentenceTransformer(_EMBED_MODEL, local_files_only=True)
        except Exception:
            logging.info(f"[Supabase] {_EMBED_MODEL} no está cacheado — descargando...")
            self.encoder = SentenceTransformer(_EMBED_MODEL)

    def _embed(self, text: str) -> list[float]:
        return self.encoder.encode(text).tolist()

    def add(self, memory_id: str, content: str, metadata: dict) -> None:
        embedding = self._embed(content)
        with _cursor(commit=True) as cur:
            # ON CONFLICT (id) no servía de nada: el id es un uuid nuevo cada vez,
            # así que "Créame una carpeta llamada Gerson es gil" acabó guardada
            # dos veces idénticas. Lo que hay que comparar es el significado, y
            # el vector ya está calculado — comprobarlo sale casi gratis.
            cur.execute(
                """
                SELECT content FROM iris_memories
                WHERE 1 - (embedding <=> %s::vector) > %s
                LIMIT 1
                """,
                (embedding, _DUPLICADO_SOBRE),
            )
            if (ya := cur.fetchone()) is not None:
                logging.debug(f"[Memoria] Ya lo sabía, no lo duplico: {ya[0][:60]}")
                return

            cur.execute(
                """
                INSERT INTO iris_memories
                    (id, content, embedding, category, importance, temporal_ref, owner, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
                    metadata.get("expires_at") or None,
                ),
            )

    def query(self, text: str, n_results: int) -> list[dict]:
        embedding = self._embed(text)
        with _cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT id, content, category, importance, temporal_ref, stored_at,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM iris_memories
                WHERE expires_at IS NULL OR expires_at >= CURRENT_DATE
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (embedding, embedding, n_results),
            )
            return [dict(r) for r in cur.fetchall()]

    def purge_expired(self) -> int:
        """Borra de verdad lo que ya caducó. Filtrar al leer no basta: si no se
        borra, la tabla crece sin techo y el índice vectorial con ella."""
        with _cursor(commit=True) as cur:
            cur.execute(
                "DELETE FROM iris_memories "
                "WHERE expires_at IS NOT NULL AND expires_at < CURRENT_DATE"
            )
            return cur.rowcount or 0

    def get_all(self) -> list[dict]:
        with _cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT id, content, category, importance, temporal_ref, stored_at, expires_at "
                "FROM iris_memories "
                "WHERE expires_at IS NULL OR expires_at >= CURRENT_DATE "
                "ORDER BY importance DESC"
            )
            return [dict(r) for r in cur.fetchall()]

    def count(self) -> int:
        with _cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM iris_memories")
            return cur.fetchone()[0]


# ─── Preference Storage ───────────────────────────────────────────────────────

_PREF_COLS = "subject, kind, valence, strength, formed_at, last_reinforced, evidence"


class SupabasePreferenceStorage(BasePreferenceStorage):

    def get_all(self) -> list[dict]:
        with _cursor(dict_rows=True) as cur:
            cur.execute(f"SELECT {_PREF_COLS} FROM iris_preferences ORDER BY strength DESC")
            return [dict(r) for r in cur.fetchall()]

    def get(self, subject: str) -> Optional[dict]:
        with _cursor(dict_rows=True) as cur:
            cur.execute(f"SELECT {_PREF_COLS} FROM iris_preferences WHERE subject = %s", (subject,))
            row = cur.fetchone()
            return dict(row) if row else None

    def save(self, preference: dict) -> None:
        with _cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO iris_preferences
                    (subject, kind, valence, strength, formed_at, last_reinforced, evidence)
                VALUES (%s, %s, %s, %s, COALESCE(%s, NOW()), NOW(), %s)
                ON CONFLICT (subject) DO UPDATE SET
                    kind            = EXCLUDED.kind,
                    valence         = EXCLUDED.valence,
                    strength        = EXCLUDED.strength,
                    last_reinforced = NOW(),
                    evidence        = EXCLUDED.evidence
                """,
                (
                    preference["subject"],
                    preference.get("kind", "tema"),
                    float(preference["valence"]),
                    float(preference["strength"]),
                    preference.get("formed_at"),
                    json.dumps(preference.get("evidence", [])),
                ),
            )

    def delete(self, subject: str) -> None:
        with _cursor(commit=True) as cur:
            cur.execute("DELETE FROM iris_preferences WHERE subject = %s", (subject,))

    def count(self) -> int:
        with _cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM iris_preferences")
            return cur.fetchone()[0]


# ─── Journal Storage ──────────────────────────────────────────────────────────

_JOURNAL_COLS = "id, at, kind, content, shared, impulse"


class SupabaseJournalStorage(BaseJournalStorage):

    def add(self, kind: str, content: str, impulse: float = 0.0) -> int:
        with _cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO iris_journal (kind, content, impulse) VALUES (%s, %s, %s) RETURNING id",
                (kind, content, float(impulse)),
            )
            return cur.fetchone()[0]

    def recent(self, n: int) -> list[dict]:
        with _cursor(dict_rows=True) as cur:
            cur.execute(f"SELECT {_JOURNAL_COLS} FROM iris_journal ORDER BY at DESC LIMIT %s", (n,))
            return [dict(r) for r in cur.fetchall()]

    def top_unshared(self) -> Optional[dict]:
        with _cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT {_JOURNAL_COLS} FROM iris_journal
                WHERE shared = FALSE
                ORDER BY impulse DESC, at DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def mark_shared(self, entry_id: int) -> None:
        with _cursor(commit=True) as cur:
            cur.execute("UPDATE iris_journal SET shared = TRUE WHERE id = %s", (entry_id,))

    def count(self) -> int:
        with _cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM iris_journal")
            return cur.fetchone()[0]


# ─── Graph Storage ────────────────────────────────────────────────────────────
#
# Grafo de conocimiento: entidades, relaciones tipadas con contexto y fecha, y
# la evolución de cada relación acumulada en `history` — lo único que la
# búsqueda vectorial no puede reconstruir.
#
# El recorrido se hace con un CTE recursivo sobre una vista que duplica cada
# arista en ambos sentidos, que es como se recorre un grafo no dirigido en SQL.
# A la escala de un asistente personal (miles de filas, profundidad máxima 2)
# todo el traversal sale en una sola ida y vuelta.


def _format_graph_rows(rows: list[dict]) -> str:
    """Formatea filas de traversal como las lee el system prompt."""
    seen: set[str] = set()
    lines: list[str] = []

    for row in rows:
        chain        = row.get("relation_chain") or []
        relation_str = " → ".join(chain) if chain else row.get("relation", "?")
        key          = f"{row['source']}-{relation_str}-{row['target']}"
        if key in seen:
            continue
        seen.add(key)

        meta: list[str] = []
        if row.get("rel_date"):
            meta.append(row["rel_date"])
        if row.get("rel_context"):
            meta.append(row["rel_context"])
        history = row.get("rel_history")
        if isinstance(history, list) and history:
            meta.append(f"antes: {history[-1]}")

        hops     = row.get("hops", 1)
        prefix   = "  └" if hops and hops > 1 else "-"
        meta_str = f" [{' | '.join(meta)}]" if meta else ""
        lines.append(f"{prefix} {row['source']} {relation_str} {row['target']}{meta_str}")

    return "\n".join(lines)


# Aristas en ambos sentidos: así el recorrido es no dirigido, como el `-[]-` de Cypher.
# {rel_filter} se sustituye por el filtro de tipos de relación, o por nada.
_WALK_SQL = """
WITH RECURSIVE edges AS (
    SELECT from_name AS a, to_name AS b, relation, context, rel_date, history
    FROM iris_relations
    {rel_filter}
    UNION ALL
    SELECT to_name AS a, from_name AS b, relation, context, rel_date, history
    FROM iris_relations
    {rel_filter}
),
walk AS (
    SELECT
        e.a AS source, e.b AS target,
        ARRAY[e.relation] AS relation_chain,
        e.context, e.rel_date, e.history,
        1 AS hops,
        ARRAY[e.a, e.b] AS visited
    FROM edges e
    WHERE e.a = %(seed)s

    UNION ALL

    SELECT
        w.source, e.b,
        w.relation_chain || e.relation,
        e.context, e.rel_date, e.history,
        w.hops + 1,
        w.visited || e.b
    FROM walk w
    JOIN edges e ON e.a = w.target
    WHERE w.hops < %(depth)s
      AND NOT (e.b = ANY(w.visited))
)
SELECT
    w.source,
    w.relation_chain,
    w.target,
    ent.type      AS target_type,
    w.context     AS rel_context,
    w.rel_date,
    w.history     AS rel_history,
    w.hops
FROM walk w
LEFT JOIN iris_entities ent ON ent.name = w.target
ORDER BY w.hops ASC, w.rel_date DESC NULLS LAST
LIMIT %(limit)s
"""


class PostgresGraphStorage(BaseGraphStorage):

    # ─── Escritura ────────────────────────────────────────────────────────────

    def add_entity(self, name: str, entity_type: str, properties: dict) -> None:
        with _cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO iris_entities (name, type, properties)
                VALUES (%s, %s, %s)
                ON CONFLICT (name) DO UPDATE SET
                    type       = EXCLUDED.type,
                    properties = iris_entities.properties || EXCLUDED.properties,
                    updated_at = NOW()
                """,
                (name, entity_type or "Unknown", json.dumps(properties or {})),
            )

    def add_relation(self, from_name: str, relation: str, to_name: str, properties: dict = None) -> None:
        """
        Crea o actualiza una relación tipada.

        Si ya existía con un contexto distinto, el anterior se acumula en
        `history` antes de sobrescribir — es la evolución de la relación en el
        tiempo, y es lo único de todo el grafo que la búsqueda vectorial no
        puede replicar.
        """
        props   = properties or {}
        context = props.get("context", "") or ""
        date    = props.get("date", "") or ""

        with _cursor(commit=True) as cur:
            # Las entidades tienen que existir antes que la arista
            cur.execute(
                "INSERT INTO iris_entities (name) VALUES (%s), (%s) ON CONFLICT (name) DO NOTHING",
                (from_name, to_name),
            )
            cur.execute(
                """
                INSERT INTO iris_relations (from_name, relation, to_name, context, rel_date)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (from_name, relation, to_name) DO UPDATE SET
                    history = iris_relations.history || (
                        CASE
                            WHEN COALESCE(iris_relations.context, '') <> ''
                             AND iris_relations.context <> EXCLUDED.context
                            THEN jsonb_build_array(
                                iris_relations.context || ' (' ||
                                COALESCE(NULLIF(iris_relations.rel_date, ''), '?') || ')'
                            )
                            ELSE '[]'::jsonb
                        END
                    ),
                    context    = CASE WHEN EXCLUDED.context  <> '' THEN EXCLUDED.context
                                      ELSE COALESCE(iris_relations.context, '') END,
                    rel_date   = CASE WHEN EXCLUDED.rel_date <> '' THEN EXCLUDED.rel_date
                                      ELSE COALESCE(iris_relations.rel_date, '') END,
                    updated_at = NOW()
                """,
                (from_name, relation, to_name, context, date),
            )

    # ─── Consultas ────────────────────────────────────────────────────────────

    def _walk(self, seed: str, relation_types: list[str], depth: int, limit: int) -> list[dict]:
        rel_filter = "WHERE relation = ANY(%(types)s)" if relation_types else ""
        sql        = _WALK_SQL.format(rel_filter=rel_filter)

        params = {"seed": seed, "depth": depth, "limit": limit}
        if relation_types:
            params["types"] = relation_types

        with _cursor(dict_rows=True) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def get_context(self, entity_name: str, depth: int = 2) -> list[dict]:
        rows = self._walk(entity_name, [], depth, limit=60)
        # get_context() expone `relation` en singular — el primer salto de la cadena
        for row in rows:
            chain = row.get("relation_chain") or []
            row["relation"] = chain[0] if chain else "?"
        return rows

    def get_deep_context(self, entity_name: str, relation_types: list[str], depth: int = 2) -> list[dict]:
        return self._walk(entity_name, relation_types, depth, limit=50)

    def get_context_by_relation(self, entity_name: str, relation_types: list[str]) -> list[dict]:
        if not relation_types:
            return self.get_context(entity_name, depth=1)
        return self._walk(entity_name, relation_types, depth=1, limit=30)

    def search_entities(self, search_term: str) -> list[dict]:
        with _cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT name, type FROM iris_entities WHERE name ILIKE '%%' || %s || '%%' LIMIT 10",
                (search_term,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_relevant_context(self, entities: list, relation_types: list, owner_name: str) -> str:
        """
        Contexto de grafo para el mensaje actual.

        Las entidades semilla se resuelven en una sola consulta; solo se vuelve
        a la base de datos si no hubo ningún resultado, primero con búsqueda
        difusa y después con el entorno inmediato del dueño.
        """
        if not entities and not relation_types:
            return ""

        all_rows: list[dict] = []
        for entity in entities:
            all_rows.extend(self.get_deep_context(entity, relation_types, depth=2))

        if not all_rows and entities:
            for entity in entities:
                for match in self.search_entities(entity):
                    all_rows.extend(self.get_deep_context(match["name"], relation_types, depth=2))

        if not all_rows and owner_name:
            all_rows = self.get_deep_context(owner_name, relation_types, depth=1)

        return _format_graph_rows(all_rows) if all_rows else ""

    def get_owner_graph(self, owner_name: str, depth: int = 1) -> str:
        rows = self.get_context(owner_name, depth)
        if not rows:
            return ""
        seen: set[str] = set()
        lines: list[str] = []
        for row in rows:
            key = f"{row['source']}-{row['relation']}-{row['target']}"
            if key in seen:
                continue
            seen.add(key)
            ctx_str = f" [{row['rel_context']}]" if row.get("rel_context") else ""
            lines.append(f"- {row['source']} {row['relation']} {row['target']}{ctx_str}")
        return "\n".join(lines)

    # ─── Utils ────────────────────────────────────────────────────────────────

    def get_entity_names(self) -> list[str]:
        with _cursor() as cur:
            cur.execute("SELECT name FROM iris_entities")
            return [r[0] for r in cur.fetchall() if r[0]]

    def counts(self) -> dict:
        """Entidades y relaciones — para /status y para diagnóstico."""
        with _cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM iris_entities")
            entities = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM iris_relations")
            relations = cur.fetchone()[0]
        return {"entities": entities, "relations": relations}

    def save(self) -> None:
        pass

    def close(self) -> None:
        pass
