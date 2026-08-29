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

    @abstractmethod
    def delete(self, memory_id: str) -> bool:
        """Olvida una memoria concreta. True si existía."""
        ...

    @abstractmethod
    def update(self, memory_id: str, content: str) -> bool:
        """
        Corrige el texto de una memoria. Reindexa el embedding, porque si no
        el vector seguiría apuntando a lo que decía antes y se recuperaría con
        las consultas equivocadas.
        """
        ...

    @abstractmethod
    def purge_expired(self) -> int:
        """
        Borra las memorias caducadas y devuelve cuántas.

        `expires_at` lo pone quien guarda: los registros de tareas nacen con
        siete días porque «te creé este archivo» importa esta semana y no en
        marzo. Las memorias de verdad no caducan — llegan con expires_at nulo.
        """
        ...


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
    def pending(self, min_impulse: float = 0.0, limit: int = 2) -> list[dict]:
        """
        Lo que aún no ha contado y más ganas tiene de contar, por impulso.

        Es lo que permite que saque en conversación algo que pensó ella, en vez
        de que el diario solo salga cuando lleva horas sin hablar con nadie.
        """
        ...

    @abstractmethod
    def mark_shared(self, entry_id: int) -> None: ...

    @abstractmethod
    def count(self) -> int: ...


class BaseEventStorage(ABC):
    """
    Qué ha hecho Iris. NO es el log del proceso.

    La diferencia importa: el log crudo ya lo guarda Docker, y meterlo en la
    misma base que la memoria añade escrituras por cada línea y un modo de fallo
    tonto — si la base se cae, pierdes justo los registros que explicarían por
    qué se cayó.

    Aquí van cosas con significado: que te escribió por iniciativa propia y por
    qué, cada entrada de diario, cada delegación con su coste, y los errores que
    hoy se traga un `except`. Sirve para responder «¿qué ha hecho esta semana?»,
    que ahora mismo no se puede contestar.

    Todo caduca. Un registro de hace tres meses no explica nada y solo engorda.
    """

    @abstractmethod
    def add(self, kind: str, summary: str, detail: dict = None, ttl_days: int = 30) -> None: ...

    @abstractmethod
    def recent(self, n: int = 30, kind: str = "") -> list[dict]: ...

    @abstractmethod
    def purge_expired(self) -> int: ...

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

    @abstractmethod
    def all_entities(self) -> list[dict]:
        """Todas las entidades, para poder mirarlas de una vez."""
        ...

    @abstractmethod
    def all_relations(self) -> list[dict]:
        """Todas las aristas, en su dirección real."""
        ...

    @abstractmethod
    def delete_entity(self, name: str) -> int:
        """Borra una entidad y, con ella, sus aristas. Devuelve cuántas cayeron."""
        ...

    @abstractmethod
    def delete_relation(self, from_name: str, relation: str, to_name: str) -> bool:
        """Borra una arista concreta y deja las entidades donde están."""
        ...

    @abstractmethod
    def rename_entity(self, old_name: str, new_name: str) -> bool:
        """
        Cambia el nombre de una entidad arrastrando sus aristas.

        Hace falta porque la extracción a veces guarda "Lucia" y "Lucía" como dos
        personas distintas, y sin esto la única salida es borrar una y perder sus
        relaciones.
        """
        ...

    def get_entity_names(self) -> list[str]:
        """
        Todos los nombres de entidad conocidos.

        Lo usa el matcher de memory.py para saber qué entidades aparecen en un
        mensaje sin preguntárselo a un LLM. Devuelve [] por defecto para que un
        backend sin soporte degrade a "no encontré ninguna" en vez de reventar.
        """
        return []