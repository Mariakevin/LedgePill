"""Priority queue for transient peek activities.

Pure Python on purpose: the queue is the only rulebook for which peek shows
next, so it can be tested without a display server.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class Activity:
    setter: Callable[..., Any]
    args: tuple = ()
    priority: int = 0
    key: str | None = None
    seq: int = 0


class ActivityQueue:
    """Highest priority first; equal priorities drain in insertion order."""

    def __init__(self, max_size: int = 4):
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        self._max_size = max_size
        self._items: list[Activity] = []
        self._next_seq = 0

    def push(self, activity: Activity) -> None:
        if activity.key is not None:
            self._items = [a for a in self._items if a.key != activity.key]
        activity.seq = self._next_seq
        self._next_seq += 1
        self._items.append(activity)
        self._evict_overflow()

    def pop_next(self) -> Activity | None:
        if not self._items:
            return None
        nxt = max(self._items, key=lambda a: (a.priority, -a.seq))
        self._items.remove(nxt)
        return nxt

    def clear(self) -> None:
        self._items.clear()

    def remove_key(self, key: str) -> None:
        self._items = [a for a in self._items if a.key != key]

    def __len__(self) -> int:
        return len(self._items)

    def _evict_overflow(self) -> None:
        while len(self._items) > self._max_size:
            lowest = min(self._items, key=lambda a: (a.priority, a.seq))
            self._items.remove(lowest)
