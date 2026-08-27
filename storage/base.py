"""
storage/base.py
Interfaces abstractas para todos los backends de storage.
"""

from abc import ABC, abstractmethod
from typing import Optional


class BaseHistoryStorage(ABC):
    @abstractmethod
    def save_message(self, role: str, content: str) -> None: ...

    @abstractmethod
    def load_recent(self, n: int) -> list[dict]: ...

    @abstractmethod
    def count(self) -> int: ...


class BaseStateStorage(ABC):
    @abstractmethod
    def save(self, data: dict) -> None: ...

    @abstractmethod
    def load(self) -> Optional[dict]: ...


class BaseVectorStorage(ABC):
    @abstractmethod
    def add(self, memory_id: str, content: str, metadata: dict) -> None: ...

    @abstractmethod
    def query(self, text: str, n_results: int) -> list[dict]: ...

    @abstractmethod
    def get_all(self) -> list[dict]: ...

    @abstractmethod
    def count(self) -> int: ...


class BasePreferenceStorage(ABC):
    """
    Gustos y aversiones que Iris se forma sola.

    Las preferencias son pocas (decenas) y se consultan en cada turno, así que
    el motor las carga enteras en memoria al arrancar y escribe de vuelta al
    cambiarlas — mismo patrón que BaseStateStorage.

    Forma del dict:
        subject          str   — "hablar de música", "que le pidan cosas de madrugada"
        kind             str   — tema | actividad | trato | entidad
        valence          float — -1.0 (le desagrada) .. +1.0 (le gusta)
        strength         float — 0.0 .. 1.0, cuánta evidencia acumulada
        formed_at        str   — ISO
        last_reinforced  str   — ISO
        evidence         list  — momentos concretos que la formaron
    """

    @abstractmethod
    def get_all(self) -> list[dict]: ...

    @abstractmethod
    def get(self, subject: str) -> Optional[dict]: ...

    @abstractmethod
    def save(self, preference: dict) -> None: ...

    @abstractmethod
    def delete(self, subject: str) -> None: ...

    @abstractmethod
    def count(self) -> int: ...


class BaseJournalStorage(ABC):
    """
    Diario de Iris — lo que hace y piensa cuando no hay nadie delante.

    Forma del dict:
        id       int   — asignado por el backend
        at       str   — ISO
        kind     str   — reflexion | conexion | curiosidad | actividad
        content  str   — en primera persona, como lo pensó
        shared   bool  — si ya se lo contó al dueño
        impulse  float — 0.0 .. 1.0, ganas de contarlo
    """

    @abstractmethod
    def add(self, kind: str, content: str, impulse: float = 0.0) -> int: ...

    @abstractmethod
    def recent(self, n: int) -> list[dict]: ...

    @abstractmethod
    def top_unshared(self) -> Optional[dict]: ...

    @abstractmethod
    def mark_shared(self, entry_id: int) -> None: ...

    @abstractmethod
    def count(self) -> int: ...


class BaseGraphStorage(ABC):
    @abstractmethod
    def add_entity(self, name: str, entity_type: str, properties: dict) -> None: ...

    @abstractmethod
    def add_relation(self, from_name: str, relation: str, to_name: str, properties: dict = None) -> None: ...

    @abstractmethod
    def get_context(self, entity_name: str, depth: int = 2) -> list[dict]: ...

    @abstractmethod
    def get_relevant_context(self, entities: list, relation_types: list, owner_name: str) -> str: ...

    @abstractmethod
    def get_owner_graph(self, owner_name: str, depth: int = 1) -> str: ...

    @abstractmethod
    def save(self) -> None: ...

    def get_entity_names(self) -> list[str]:
        """
        Todos los nombres de entidad conocidos.

        Lo usa el matcher de memory.py para saber qué entidades aparecen en un
        mensaje sin preguntárselo a un LLM. Devuelve [] por defecto para que un
        backend sin soporte degrade a "no encontré ninguna" en vez de reventar.
        """
        return []