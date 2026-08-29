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
from datetime import datetime, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from config.settings import settings
from storage.base import (
    BaseEventStorage,
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

        # Eventos — qué ha hecho Iris, no el log crudo del proceso
        cur.execute("""
            CREATE TABLE IF NOT EXISTS iris_events (
                id         BIGSERIAL PRIMARY KEY,
                at         TIMESTAMPTZ DEFAULT NOW(),
                kind       TEXT NOT NULL,
                summary    TEXT NOT NULL,
                detail     JSONB DEFAULT '{}',
                expires_at DATE
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_recientes
            ON iris_events (at DESC);
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


_ENCODER = None
_ENCODER_LOCK = threading.Lock()


def _get_encoder():
    """
    El modelo de embeddings, uno para todo el proceso.

    Lo usan las memorias y el diario. Cargarlo dos veces son otros 90 MB de RAM
    y otro arranque lento, y en la VM eso sí se nota.

    Con el modelo ya cacheado, SentenceTransformer hace ~25 peticiones a
    HuggingFace solo para comprobar si hay versión nueva. En un servidor eso es
    latencia de arranque y una dependencia externa innecesaria, así que se
    intenta primero en local y solo se sale a la red si no está.
    """
    global _ENCODER
    if _ENCODER is not None:
        return _ENCODER
    with _ENCODER_LOCK:
        if _ENCODER is None:
            from sentence_transformers import SentenceTransformer
            try:
                _ENCODER = SentenceTransformer(_EMBED_MODEL, local_files_only=True)
            except Exception:
                logging.info(f"[Supabase] {_EMBED_MODEL} no está cacheado — descargando...")
                _ENCODER = SentenceTransformer(_EMBED_MODEL)
    return _ENCODER


def _embed_text(text: str) -> list[float]:
    return _get_encoder().encode(text).tolist()


class SupabaseVectorStorage(BaseVectorStorage):

    def __init__(self):
        self.encoder = _get_encoder()

    def _embed(self, text: str) -> list[float]:
        return _embed_text(text)

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

    def delete(self, memory_id: str) -> bool:
        with _cursor(commit=True) as cur:
            cur.execute("DELETE FROM iris_memories WHERE id = %s", (memory_id,))
            return (cur.rowcount or 0) > 0

    def update(self, memory_id: str, content: str) -> bool:
        # Se reindexa el embedding: si no, el vector seguiría apuntando al texto
        # viejo y la memoria corregida se recuperaría con las consultas de antes.
        embedding = self._embed(content)
        with _cursor(commit=True) as cur:
            cur.execute(
                "UPDATE iris_memories SET content = %s, embedding = %s WHERE id = %s",
                (content, embedding, memory_id),
            )
            return (cur.rowcount or 0) > 0

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


# ─── Event Storage ────────────────────────────────────────────────────────────

_EVENT_COLS = "id, at, kind, summary, detail, expires_at"


class SupabaseEventStorage(BaseEventStorage):

    def add(self, kind: str, summary: str, detail: dict = None, ttl_days: int = 30) -> None:
        caduca = (datetime.now() + timedelta(days=ttl_days)).date() if ttl_days else None
        with _cursor(commit=True) as cur:
            cur.execute(
                "INSERT INTO iris_events (kind, summary, detail, expires_at) VALUES (%s,%s,%s,%s)",
                (kind, summary, json.dumps(detail or {}), caduca),
            )

    def recent(self, n: int = 30, kind: str = "") -> list[dict]:
        filtro = "WHERE kind = %s" if kind else ""
        params = ([kind, n] if kind else [n])
        with _cursor(dict_rows=True) as cur:
            cur.execute(
                f"SELECT {_EVENT_COLS} FROM iris_events {filtro} ORDER BY at DESC LIMIT %s",
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def purge_expired(self) -> int:
        with _cursor(commit=True) as cur:
            cur.execute(
                "DELETE FROM iris_events WHERE expires_at IS NOT NULL AND expires_at < CURRENT_DATE"
            )
            return cur.rowcount or 0

    def count(self) -> int:
        with _cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM iris_events")
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

    def pending(self, min_impulse: float = 0.0, limit: int = 2) -> list[dict]:
        """
        Lo que aún no ha contado y más ganas tiene de contar.

        Ordenado por impulso, no por parecido con lo que se esté hablando: medido
        con este encoder, "que tal el día" se parece MÁS a una entrada sobre su
        novela (0.529) que "llevo semanas sin tocar la novela" (0.463). Está
        entrenado en inglés y en español lo comprime todo entre 0.4 y 0.6, así
        que no hay umbral que separe. Decidir si algo viene a cuento se lo queda
        el modelo que ya está respondiendo, que entiende español de sobra.
        """
        with _cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT {_JOURNAL_COLS} FROM iris_journal
                WHERE shared = FALSE AND impulse >= %s
                ORDER BY impulse DESC, at DESC
                LIMIT %s
                """,
                (float(min_impulse), limit),
            )
            return [dict(r) for r in cur.fetchall()]

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


def _format_graph_rows(rows: list[dict], limite: int = 14) -> str:
    """
    Formatea el traversal para el system prompt, con techo.

    El techo no es cosmético. El recorrido a profundidad 2 cruza cada vecino con
    todos los demás, así que un grafo de quince aristas producía cincuenta líneas
    del tipo "Matias LE_ABURRE → SE_SIENTE Aburrimiento", que no es un camino con
    sentido: son dos aristas sin relación encadenadas por un nodo común.

    El daño era real y medido: ese bloque llegó a ocupar 6.000 caracteres —más
    que toda su personalidad— repitiendo nueve veces "Iris odia la alabanza". Con
    eso delante rechazaba cualquier cosa amable que le dijeran. Lo que la volvió
    seca no fue su carácter: era el ruido tapándolo.
    """
    vistos: set = set()
    directas: list[str] = []
    indirectas: list[str] = []

    for row in rows:
        chain        = row.get("relation_chain") or []
        relation_str = " → ".join(chain) if chain else row.get("relation", "?")
        hops         = row.get("hops", 1)

        origen, destino = row["source"], row["target"]
        if row.get("invertida") and hops == 1:
            origen, destino = destino, origen

        # Se deduplica por el HECHO, no por el camino: da igual por cuántas rutas
        # se llegue a "Iris odia la alabanza", sigue siendo una sola cosa.
        clave = (origen, relation_str, destino) if hops == 1 else (destino,)
        if clave in vistos:
            continue
        vistos.add(clave)

        meta: list[str] = []
        if row.get("rel_date"):
            meta.append(row["rel_date"])
        if row.get("rel_context"):
            meta.append(row["rel_context"])
        history = row.get("rel_history")
        if isinstance(history, list) and history:
            meta.append(f"antes: {history[-1]}")

        meta_str = f" [{' | '.join(meta)}]" if meta else ""
        linea    = f"- {origen} {relation_str} {destino}{meta_str}"
        (directas if hops == 1 else indirectas).append(linea)

    # Las de un salto primero: son afirmaciones. Las de dos solo sugieren que dos
    # cosas están conectadas, y entran únicamente si sobra sitio.
    return "\n".join(directas[:limite] + indirectas[: max(0, limite - len(directas))])


_WALK_SQL = """
WITH RECURSIVE edges AS (
    -- Las aristas se recorren en los dos sentidos: para llegar a Lucía desde
    -- Halcón hace falta poder ir en contra de la flecha. Pero `invertida` deja
    -- constancia de cuál era la dirección real, o al renderizar saldría
    -- "Halcón DESARROLLA Lucía", que es la frase al revés.
    SELECT from_name AS a, to_name AS b, relation, context, rel_date, history,
           FALSE AS invertida
    FROM iris_relations
    {rel_filter}
    UNION ALL
    SELECT to_name AS a, from_name AS b, relation, context, rel_date, history,
           TRUE AS invertida
    FROM iris_relations
    {rel_filter}
),
walk AS (
    SELECT
        e.a AS source, e.b AS target,
        ARRAY[e.relation] AS relation_chain,
        e.context, e.rel_date, e.history,
        1 AS hops,
        e.invertida,
        ARRAY[e.a, e.b] AS visited
    FROM edges e
    WHERE e.a = %(seed)s

    UNION ALL

    SELECT
        w.source, e.b,
        w.relation_chain || e.relation,
        e.context, e.rel_date, e.history,
        w.hops + 1,
        w.invertida,
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
    w.invertida,
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

    def all_entities(self) -> list[dict]:
        with _cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT e.name, e.type, e.updated_at,
                       (SELECT COUNT(*) FROM iris_relations r
                         WHERE r.from_name = e.name OR r.to_name = e.name) AS grado
                FROM iris_entities e
                ORDER BY grado DESC, e.name
            """)
            return [dict(r) for r in cur.fetchall()]

    def all_relations(self) -> list[dict]:
        with _cursor(dict_rows=True) as cur:
            cur.execute("""
                SELECT from_name, relation, to_name, context, rel_date, history
                FROM iris_relations ORDER BY from_name, relation
            """)
            return [dict(r) for r in cur.fetchall()]

    def delete_entity(self, name: str) -> int:
        with _cursor(commit=True) as cur:
            cur.execute(
                "SELECT COUNT(*) FROM iris_relations WHERE from_name = %s OR to_name = %s",
                (name, name),
            )
            aristas = cur.fetchone()[0]
            # Las aristas se van solas: la FK lleva ON DELETE CASCADE.
            cur.execute("DELETE FROM iris_entities WHERE name = %s", (name,))
            return aristas if cur.rowcount else -1

    def delete_relation(self, from_name: str, relation: str, to_name: str) -> bool:
        with _cursor(commit=True) as cur:
            cur.execute(
                "DELETE FROM iris_relations WHERE from_name=%s AND relation=%s AND to_name=%s",
                (from_name, relation, to_name),
            )
            return (cur.rowcount or 0) > 0

    def rename_entity(self, old_name: str, new_name: str) -> bool:
        """
        Renombra arrastrando las aristas. Si el nombre nuevo ya existe, fusiona.

        Fusionar es el caso normal, no el raro: la extracción guarda "Lucia" y
        "Lucía" como dos personas distintas, y lo que quieres es juntarlas sin
        perder las relaciones de ninguna de las dos.
        """
        with _cursor(commit=True) as cur:
            cur.execute("SELECT 1 FROM iris_entities WHERE name = %s", (old_name,))
            if cur.fetchone() is None:
                return False

            cur.execute(
                "INSERT INTO iris_entities (name, type) "
                "SELECT %s, type FROM iris_entities WHERE name = %s "
                "ON CONFLICT (name) DO NOTHING",
                (new_name, old_name),
            )

            # Primero fuera las que quedarían repetidas al renombrar; si no, el
            # UPDATE choca con la clave primaria (from_name, relation, to_name).
            for col, otro in (("from_name", "to_name"), ("to_name", "from_name")):
                cur.execute(
                    f"""DELETE FROM iris_relations a
                         WHERE a.{col} = %s
                           AND EXISTS (SELECT 1 FROM iris_relations b
                                        WHERE b.{col} = %s
                                          AND b.relation = a.relation
                                          AND b.{otro}   = a.{otro})""",
                    (old_name, new_name),
                )
                cur.execute(
                    f"UPDATE iris_relations SET {col} = %s WHERE {col} = %s",
                    (new_name, old_name),
                )

            cur.execute("DELETE FROM iris_entities WHERE name = %s", (old_name,))
            return True

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
