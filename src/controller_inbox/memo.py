"""Small bounded caches for work that is a pure function of its inputs (a file's text, its bytes' SHA-256).

Every key holds what the result depends on (the text itself, or the content's hash), never an id alone, so a
changed file or a page fixed since can't be served an old result. Safe to share between threads.
"""

from __future__ import annotations

import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

_MISSING = object()
_ALL: weakref.WeakSet[LRU] = weakref.WeakSet()


class LRU:
    """At most ``size`` results, the least recently used dropped first."""

    def __init__(self, size: int):
        self.size = size
        self._items: OrderedDict[Hashable, Any] = OrderedDict()
        self._lock = threading.Lock()
        _ALL.add(self)

    def get(self, key: Hashable, default: Any = None) -> Any:
        with self._lock:
            value = self._items.get(key, _MISSING)
            if value is _MISSING:
                return default
            self._items.move_to_end(key)
            return value

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)

    def get_or(self, key: Hashable, make: Callable[[], Any]) -> Any:
        """The kept result for ``key``, else ``make()``'s (worked out outside the lock, and kept)."""
        value = self.get(key, _MISSING)
        if value is _MISSING:
            value = make()
            self.put(key, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


def clear_all() -> None:
    """Forget every kept result (tests start from nothing)."""
    for cache in list(_ALL):
        cache.clear()
