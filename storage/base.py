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


class BaseGraphStorage(ABC):
    @abstractmethod
    def add_entity(self, name: str, entity_type: str, properties: dict) -> None: ...

    @abstractmethod
    def add_relation(self, from_name: str, relation: str, to_name: str, properties: dict = None) -> None: ...
    
    @abstractmethod
    def get_context(self, entity_name: str, depth: int = 2) -> list[dict]: ...

    @abstractmethod
    def save(self) -> None: ...