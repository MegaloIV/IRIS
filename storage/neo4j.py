"""
storage/neo4j.py
Implementación de grafo de conocimiento usando Neo4j AuraDB.
(Actualizado con auto-reconexión y silenciador de warnings)
"""

import logging
from typing import Optional

from neo4j import GraphDatabase

from config.settings import settings
from storage.base import BaseGraphStorage

# Silenciar el spam de advertencias de Neo4j cuando una relación no existe en el grafo
logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)

class Neo4jGraphStorage(BaseGraphStorage):

    def __init__(self):
        self.driver = GraphDatabase.driver(
            settings.storage.neo4j_uri,
            auth=(settings.storage.neo4j_user, settings.storage.neo4j_password),
            # Ajustes recomendados para mantener conexiones en AuraDB
            max_connection_lifetime=30 * 60,
            keep_alive=True
        )
        self._init_constraints()
        logging.info("[Neo4j] Conectado a AuraDB.")

    def _init_constraints(self):
        def _create_constraint(tx):
            tx.run(
                "CREATE CONSTRAINT entity_name IF NOT EXISTS "
                "FOR (e:Entity) REQUIRE e.name IS UNIQUE"
            )
        with self.driver.session() as session:
            session.execute_write(_create_constraint)

    def close(self):
        self.driver.close()

    # ─── Escritura ────────────────────────────────────────────────────────────

    def add_entity(self, name: str, entity_type: str, properties: dict) -> None:
        def _add_entity_tx(tx):
            tx.run(
                """
                MERGE (e:Entity {name: $name})
                SET e.type = $type, e.updated_at = datetime()
                SET e += $properties
                """,
                name=name, type=entity_type, properties=properties,
            )
        with self.driver.session() as session:
            session.execute_write(_add_entity_tx)

    def add_relation(self, from_name: str, relation: str, to_name: str, properties: dict = None) -> None:
        def _add_relation_tx(tx):
            tx.run(
                f"""
                MERGE (a:Entity {{name: $from_name}})
                MERGE (b:Entity {{name: $to_name}})
                MERGE (a)-[r:{relation}]->(b)
                SET r.created_at = datetime()
                """,
                from_name=from_name, to_name=to_name,
            )
        with self.driver.session() as session:
            session.execute_write(_add_relation_tx)

    # ─── Consultas ────────────────────────────────────────────────────────────

    def get_context(self, entity_name: str, depth: int = 2) -> list[dict]:
        def _get_context_tx(tx):
            result = tx.run(
                f"""
                MATCH path = (e:Entity {{name: $name}})-[*1..{depth}]-(related)
                RETURN
                    e.name AS source,
                    type(relationships(path)[0]) AS relation,
                    related.name AS target,
                    related.type AS target_type
                LIMIT 50
                """,
                name=entity_name,
            )
            return [dict(r) for r in result]
            
        with self.driver.session() as session:
            return session.execute_read(_get_context_tx)

    def get_context_by_relation(self, entity_name: str, relation_types: list[str]) -> list[dict]:
        if not relation_types:
            return self.get_context(entity_name, depth=1)

        relation_filter = "|".join(relation_types)
        
        def _get_ctx_rel_tx(tx):
            result = tx.run(
                f"""
                MATCH (e:Entity {{name: $name}})-[r:{relation_filter}]-(related)
                RETURN
                    e.name AS source,
                    type(r) AS relation,
                    related.name AS target,
                    related.type AS target_type,
                    r.context AS rel_context,
                    r.date AS rel_date
                LIMIT 30
                """,
                name=entity_name,
            )
            return [dict(r) for r in result]
            
        with self.driver.session() as session:
            return session.execute_read(_get_ctx_rel_tx)

    def search_entities(self, search_term: str) -> list[dict]:
        def _search_entities_tx(tx):
            result = tx.run(
                """
                MATCH (e:Entity)
                WHERE toLower(e.name) CONTAINS toLower($search_term)
                RETURN e.name AS name, e.type AS type
                LIMIT 10
                """,
                search_term=search_term,
            )
            return [dict(r) for r in result]
            
        with self.driver.session() as session:
            return session.execute_read(_search_entities_tx)

    def get_relevant_context(
        self,
        entities: list[str],
        relation_types: list[str],
        owner_name: str,
    ) -> str:
        if not entities and not relation_types:
            return ""

        results = []

        for entity in entities:
            rows = self.get_context_by_relation(entity, relation_types)
            if not rows:
                matches = self.search_entities(entity)
                for match in matches:
                    rows += self.get_context_by_relation(match["name"], relation_types)
            results.extend(rows)

        if not results and relation_types:
            results = self.get_context_by_relation(owner_name, relation_types)

        if not results:
            return ""

        seen  = set()
        lines = []
        for row in results:
            key = f"{row['source']}-{row['relation']}-{row['target']}"
            if key not in seen:
                seen.add(key)
                
                # Construir metadatos si existen
                meta = []
                if row.get('rel_date'): meta.append(f"Fecha: {row['rel_date']}")
                if row.get('rel_context'): meta.append(f"Detalle: {row['rel_context']}")
                
                meta_str = f" ({' | '.join(meta)})" if meta else ""
                lines.append(f"- {row['source']} {row['relation']} {row['target']}{meta_str}")

        return "\n".join(lines)

    def get_owner_graph(self, owner_name: str, depth: int = 1) -> str:
        context = self.get_context(owner_name, depth)
        if not context:
            return ""

        seen  = set()
        lines = []
        for row in context:
            key = f"{row['source']}-{row['relation']}-{row['target']}"
            if key not in seen:
                seen.add(key)
                lines.append(f"- {row['source']} {row['relation']} {row['target']}")

        return "\n".join(lines)

    def save(self) -> None:
        pass