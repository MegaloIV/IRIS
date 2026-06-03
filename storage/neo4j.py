"""
storage/neo4j.py
Grafo de conocimiento Neo4j AuraDB.

Mejoras respecto a la versión anterior:
- add_relation almacena context y date en la relación (bug corregido)
- Evolución: el contexto anterior se acumula en r.history antes de sobrescribir
- Traversal a profundidad 2: entidades vecinas enriquecen el contexto
- get_relevant_context devuelve relaciones con contexto psicológico completo
"""

import logging

from neo4j import GraphDatabase

from config.settings import settings
from storage.base import BaseGraphStorage

logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)


class Neo4jGraphStorage(BaseGraphStorage):

    def __init__(self):
        self.driver = GraphDatabase.driver(
            settings.storage.neo4j_uri,
            auth=(settings.storage.neo4j_user, settings.storage.neo4j_password),
            max_connection_lifetime=30 * 60,
            keep_alive=True,
        )
        self._init_constraints()
        logging.info("[Neo4j] Conectado a AuraDB.")

    def _init_constraints(self):
        def _tx(tx):
            tx.run(
                "CREATE CONSTRAINT entity_name IF NOT EXISTS "
                "FOR (e:Entity) REQUIRE e.name IS UNIQUE"
            )
        with self.driver.session() as session:
            session.execute_write(_tx)

    def close(self):
        self.driver.close()

    # ─── Escritura ────────────────────────────────────────────────────────────

    def add_entity(self, name: str, entity_type: str, properties: dict) -> None:
        def _tx(tx):
            tx.run(
                """
                MERGE (e:Entity {name: $name})
                SET e.type = $type, e.updated_at = datetime()
                SET e += $properties
                """,
                name=name, type=entity_type, properties=properties,
            )
        with self.driver.session() as session:
            session.execute_write(_tx)

    def add_relation(self, from_name: str, relation: str, to_name: str, properties: dict = None) -> None:
        """
        Crea o actualiza una relación tipada entre dos entidades.
        - Las propiedades 'context' y 'date' se almacenan en la relación.
        - Si ya existía con un contexto diferente, el contexto anterior se
          acumula en r.history antes de sobrescribir (evolución de la relación).
        """
        props   = properties or {}
        context = props.get("context", "")
        date    = props.get("date", "")

        def _tx(tx):
            tx.run(
                f"""
                MERGE (a:Entity {{name: $from_name}})
                MERGE (b:Entity {{name: $to_name}})
                MERGE (a)-[r:{relation}]->(b)
                ON CREATE SET
                    r.context    = $context,
                    r.date       = $date,
                    r.created_at = datetime(),
                    r.history    = []
                ON MATCH SET
                    r.history    = coalesce(r.history, []) +
                        CASE
                            WHEN r.context IS NOT NULL
                             AND r.context <> ''
                             AND r.context <> $context
                            THEN [r.context + ' (' + coalesce(r.date, '?') + ')']
                            ELSE []
                        END,
                    r.context    = CASE WHEN $context <> '' THEN $context ELSE coalesce(r.context, '') END,
                    r.date       = CASE WHEN $date   <> '' THEN $date   ELSE coalesce(r.date,    '') END,
                    r.updated_at = datetime()
                """,
                from_name=from_name, to_name=to_name, context=context, date=date,
            )
        with self.driver.session() as session:
            session.execute_write(_tx)

    # ─── Consultas ────────────────────────────────────────────────────────────

    def get_context(self, entity_name: str, depth: int = 2) -> list[dict]:
        """Relaciones directas de una entidad hasta `depth` saltos."""
        def _tx(tx):
            result = tx.run(
                f"""
                MATCH path = (e:Entity {{name: $name}})-[*1..{depth}]-(related)
                WITH e, relationships(path)[0] AS r, related
                RETURN
                    e.name       AS source,
                    type(r)      AS relation,
                    related.name AS target,
                    related.type AS target_type,
                    r.context    AS rel_context,
                    r.date       AS rel_date,
                    r.history    AS rel_history
                LIMIT 60
                """,
                name=entity_name,
            )
            return [dict(row) for row in result]
        with self.driver.session() as session:
            return session.execute_read(_tx)

    def get_deep_context(self, entity_name: str, relation_types: list[str], depth: int = 2) -> list[dict]:
        """
        Traversal a profundidad `depth`.
        Devuelve la cadena completa de relaciones para cada camino, no solo el
        primer salto. Para un mismo target alcanzado por dos caminos, se
        devuelven ambos (el más corto primero).
        """
        type_clause = f":{('|'.join(relation_types))}" if relation_types else ""

        def _tx(tx):
            result = tx.run(
                f"""
                MATCH path = (e:Entity {{name: $name}})-[{type_clause}*1..{depth}]-(related)
                WITH e, relationships(path) AS rels, related, length(path) AS hops
                RETURN
                    e.name                  AS source,
                    [r IN rels | type(r)]   AS relation_chain,
                    related.name            AS target,
                    related.type            AS target_type,
                    rels[-1].context        AS rel_context,
                    rels[-1].date           AS rel_date,
                    rels[-1].history        AS rel_history,
                    hops
                ORDER BY hops ASC, rel_date DESC
                LIMIT 50
                """,
                name=entity_name,
            )
            return [dict(row) for row in result]
        with self.driver.session() as session:
            return session.execute_read(_tx)

    def get_context_by_relation(self, entity_name: str, relation_types: list[str]) -> list[dict]:
        """Profundidad 1 filtrada por tipos de relación."""
        if not relation_types:
            return self.get_context(entity_name, depth=1)
        relation_filter = "|".join(relation_types)

        def _tx(tx):
            result = tx.run(
                f"""
                MATCH (e:Entity {{name: $name}})-[r:{relation_filter}]-(related)
                RETURN
                    e.name       AS source,
                    type(r)      AS relation,
                    related.name AS target,
                    related.type AS target_type,
                    r.context    AS rel_context,
                    r.date       AS rel_date,
                    r.history    AS rel_history
                LIMIT 30
                """,
                name=entity_name,
            )
            return [dict(row) for row in result]
        with self.driver.session() as session:
            return session.execute_read(_tx)

    def search_entities(self, search_term: str) -> list[dict]:
        """Búsqueda fuzzy de entidades por nombre (substring, case-insensitive)."""
        def _tx(tx):
            result = tx.run(
                """
                MATCH (e:Entity)
                WHERE toLower(e.name) CONTAINS toLower($term)
                RETURN e.name AS name, e.type AS type
                LIMIT 10
                """,
                term=search_term,
            )
            return [dict(row) for row in result]
        with self.driver.session() as session:
            return session.execute_read(_tx)

    def get_relevant_context(
        self,
        entities: list[str],
        relation_types: list[str],
        owner_name: str,
    ) -> str:
        """
        Recupera el contexto de grafo más relevante para el mensaje actual.

        Para cada entidad mencionada:
        1. Busca relaciones directas e indirectas (depth 2).
        2. Si no encuentra nada, intenta búsqueda fuzzy y reintenta.
        3. Formatea con cadena de relaciones, contexto psicológico, fecha
           e historial de evolución de la relación.
        """
        if not entities and not relation_types:
            return ""

        seen: set[str] = set()
        all_rows: list[dict] = []

        for entity in entities:
            rows = self.get_deep_context(entity, relation_types, depth=2)
            if not rows:
                matches = self.search_entities(entity)
                for match in matches:
                    rows += self.get_deep_context(match["name"], relation_types, depth=2)
            all_rows.extend(rows)

        if not all_rows:
            all_rows = self.get_deep_context(owner_name, relation_types, depth=1)

        if not all_rows:
            return ""

        lines: list[str] = []
        for row in all_rows:
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

    def get_owner_graph(self, owner_name: str, depth: int = 1) -> str:
        context = self.get_context(owner_name, depth)
        if not context:
            return ""
        seen: set[str] = set()
        lines: list[str] = []
        for row in context:
            key = f"{row['source']}-{row['relation']}-{row['target']}"
            if key in seen:
                continue
            seen.add(key)
            ctx_str = f" [{row['rel_context']}]" if row.get("rel_context") else ""
            lines.append(f"- {row['source']} {row['relation']} {row['target']}{ctx_str}")
        return "\n".join(lines)

    def save(self) -> None:
        pass
