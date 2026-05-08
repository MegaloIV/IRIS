"""
storage/neo4j.py
Implementación de grafo de conocimiento usando Neo4j AuraDB.
"""

import logging
from typing import Optional

from neo4j import GraphDatabase

from config.settings import settings
from storage.base import BaseGraphStorage


class Neo4jGraphStorage(BaseGraphStorage):

    def __init__(self):
        self.driver = GraphDatabase.driver(
            settings.storage.neo4j_uri,
            auth=(settings.storage.neo4j_user, settings.storage.neo4j_password),
        )
        self._init_constraints()
        logging.info("[Neo4j] Conectado a AuraDB.")

    def _init_constraints(self):
        with self.driver.session() as session:
            session.run(
                "CREATE CONSTRAINT entity_name IF NOT EXISTS "
                "FOR (e:Entity) REQUIRE e.name IS UNIQUE"
            )

    def close(self):
        self.driver.close()

    # ─── Escritura ────────────────────────────────────────────────────────────

    def add_entity(self, name: str, entity_type: str, properties: dict) -> None:
        with self.driver.session() as session:
            session.run(
                """
                MERGE (e:Entity {name: $name})
                SET e.type = $type, e.updated_at = datetime()
                SET e += $properties
                """,
                name=name, type=entity_type, properties=properties,
            )

    def add_relation(self, from_name: str, relation: str, to_name: str) -> None:
        with self.driver.session() as session:
            session.run(
                f"""
                MERGE (a:Entity {{name: $from_name}})
                MERGE (b:Entity {{name: $to_name}})
                MERGE (a)-[r:{relation}]->(b)
                SET r.created_at = datetime()
                """,
                from_name=from_name, to_name=to_name,
            )

    # ─── Consultas ────────────────────────────────────────────────────────────

    def get_context(self, entity_name: str, depth: int = 2) -> list[dict]:
        with self.driver.session() as session:
            result = session.run(
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

    def get_context_by_relation(self, entity_name: str, relation_types: list[str]) -> list[dict]:
        if not relation_types:
            return self.get_context(entity_name, depth=1)

        relation_filter = "|".join(relation_types)
        with self.driver.session() as session:
            result = session.run(
                f"""
                MATCH (e:Entity {{name: $name}})-[r:{relation_filter}]-(related)
                RETURN
                    e.name AS source,
                    type(r) AS relation,
                    related.name AS target,
                    related.type AS target_type
                LIMIT 20
                """,
                name=entity_name,
            )
            return [dict(r) for r in result]

    def search_entities(self, search_term: str) -> list[dict]:
        """Busca entidades por nombre parcial."""
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (e:Entity)
                WHERE toLower(e.name) CONTAINS toLower($search_term)
                RETURN e.name AS name, e.type AS type
                LIMIT 10
                """,
                search_term=search_term,
            )
            return [dict(r) for r in result]

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
                lines.append(f"- {row['source']} {row['relation']} {row['target']}")

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