"""Tiny TTL memo for values the whole site re-derives on every request.

Only *derived, whole-corpus* values live here — dropdown option lists, summary
counts, dashboard aggregates. Search results are deliberately never cached: this
is an evidence archive and a query must always read the database it claims to be
reporting on.

The lock is held across the recompute rather than just the dictionary write.
That serialises a stampede of concurrent misses into one query, which is the
point — the queries being memoised are whole-table scans, so ten threads running
them at once is exactly the situation worth preventing.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

_lock = threading.Lock()
_store: dict[Any, tuple[float, Any]] = {}


def cached(key: Any, ttl: float, compute: Callable[[], Any]) -> Any:
    """Return ``compute()``'s value for ``key``, recomputing at most every ttl."""
    now = time.monotonic()
    hit = _store.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    with _lock:
        hit = _store.get(key)
        now = time.monotonic()
        if hit is not None and hit[0] > now:
            return hit[1]
        value = compute()
        _store[key] = (now + ttl, value)
        return value


def clear() -> None:
    """Drop every memoised value (tests)."""
    with _lock:
        _store.clear()


# Option lists and corpus-wide counts move only when the scraper commits a batch,
# and a stale dropdown entry costs nothing; a stale *search result* would, which
# is why nothing on that path is memoised.
OPTIONS_TTL = 300.0
STATS_TTL = 60.0
DASHBOARD_TTL = 30.0
