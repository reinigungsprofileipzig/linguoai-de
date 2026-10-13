"""Kleiner threadsicherer LRU-Cache für wiederholte Übersetzungen."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Hashable
from threading import RLock
from typing import Generic, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class LRUCache(Generic[K, V]):
    def __init__(self, max_size: int = 2_000) -> None:
        if max_size < 1:
            raise ValueError("max_size muss positiv sein")
        self._max_size = max_size
        self._values: OrderedDict[K, V] = OrderedDict()
        self._lock = RLock()

    def get(self, key: K) -> V | None:
        with self._lock:
            try:
                value = self._values.pop(key)
            except KeyError:
                return None
            self._values[key] = value
            return value

    def set(self, key: K, value: V) -> None:
        with self._lock:
            self._values.pop(key, None)
            self._values[key] = value
            while len(self._values) > self._max_size:
                self._values.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)
