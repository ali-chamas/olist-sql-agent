"""What keeps a public demo on a free API key alive: rate limit, answer cache, daily token budget.

All three are in memory and thread-safe. That is enough for one process; a
multi-instance deployment would move them to Redis.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import threading
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable
from typing import Any


class RateLimiter:
    """Sliding-window limit per client."""

    def __init__(self, limit: int, window_s: float = 60.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.window_s, self.clock = limit, window_s, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            now = self.clock()
            hits = self._hits[key]
            while hits and now - hits[0] >= self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


def normalize_question(question: str) -> str:
    """Lower-case, collapse spaces, drop trailing punctuation: 'How many orders ?' == 'how many orders'."""
    return re.sub(r"\s+", " ", question.strip().lower()).rstrip(" ?!.")


class AnswerCache:
    """LRU cache with a time-to-live. Keys cover the model, the question and the conversation so far."""

    def __init__(
        self, max_entries: int = 500, ttl_s: float = 86_400, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.max_entries, self.ttl_s, self.clock = max_entries, ttl_s, clock
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    @staticmethod
    def key(model: str, question: str, history: list[dict[str, str]] | None = None) -> str:
        parts = [model, normalize_question(question)]
        parts += [f"{normalize_question(t.get('question', ''))}|{t.get('sql', '')}" for t in history or []]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._items.get(key)
            if item is None or self.clock() - item[0] >= self.ttl_s:
                self._items.pop(key, None)
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return item[1]

    def put(self, key: str, value: Any) -> None:
        if self.max_entries <= 0:
            return
        with self._lock:
            self._items[key] = (self.clock(), value)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)


class DailyTokenBudget:
    """Caps the tokens spent per UTC day. A limit of 0 means no cap."""

    def __init__(self, limit: int, today: Callable[[], dt.date] | None = None) -> None:
        self.limit = limit
        self._today = today or (lambda: dt.datetime.now(dt.UTC).date())
        self._day = self._today()
        self._used = 0
        self._lock = threading.Lock()

    def _roll(self) -> None:
        day = self._today()
        if day != self._day:
            self._day, self._used = day, 0

    def available(self) -> bool:
        with self._lock:
            self._roll()
            return self.limit <= 0 or self._used < self.limit

    def spend(self, tokens: int) -> None:
        with self._lock:
            self._roll()
            self._used += max(0, tokens)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            self._roll()
            return {"limit": self.limit, "used": min(self._used, self.limit) if self.limit else self._used}
