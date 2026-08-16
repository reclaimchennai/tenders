"""Frequent, lightweight retry sweep for tenders whose documents are incomplete.

The gap this closes: a tender is discoverable — and, per watches.py, can
already be *notified about* — the moment the fast "latest" listing poll sees it
(minutes after the portal posts it). Actually reading its detail page and
downloading its documents is separate work, and until now the only thing doing
it was ``forward_capture.run_forward``'s once-a-cycle pass. A cycle processes
its whole row list before looping, so a tender that appears mid-cycle — with no
urgent deadline to jump the queue via ``latest_active._capture_urgent`` — could
sit for the rest of that cycle's runtime, which for a several-thousand-row
live queue is measured in hours. The portal deletes documents at close, so
hours of exposure on a tender nobody has told to wait is exactly the risk this
archive exists to avoid.

This module is that missing "tell it to wait, on a schedule" layer: a small,
cheap DB query — no listing captcha, no full enumeration — that finds tenders
due for another capture attempt right now and drives them through the same
``fetch_and_store_detail`` every other capture path uses, on the progressive
schedule in ``doc_lifecycle.next_attempt_after`` (1m, 5m, 15m, 30m, then
hourly). It is ticked from the same call sites as ``latest_active.LatestWatch``
— inside ``forward_capture``'s row loop and during ``continuous._pause`` — so
it runs at whatever cadence those already check in at (a few seconds), far
finer than the schedule itself needs, which is exactly what makes 1-minute and
5-minute retries actually land on time rather than being a lower bound nobody
reaches.

Eligibility is deliberately narrow, mirroring ``forward_capture.run_forward``'s
own ``is_open`` (see its comment): ``closing_date`` must be known and still in
the future. A NULL closing_date must NEVER count as open here — that exact
mistake once let 62,334 undated backfill rows flood the unbounded "open"
bucket and grew a single forward_capture cycle to 80,813 rows over two days.
Undated or already-closed tenders are out of scope for this fast path; they
are recovered, unhurried, by the ration in forward_capture instead.
"""

from __future__ import annotations

import logging
import time

from .doc_lifecycle import next_attempt_after
from .http_client import RequestCapExceeded

log = logging.getLogger("capture_retry")

_DEFAULTS = {
    "enabled": True,
    # How often this sweep itself is allowed to run. Deliberately much shorter
    # than the retry schedule's own steps (1m) so that "due at t+60s" actually
    # fires within a few seconds of t+60s rather than whenever the next big
    # cycle happens to reach it.
    "check_interval_s": 20,
    # Tenders taken per sweep. Small: this runs dozens of times an hour, so a
    # deep backlog drains quickly without ever taking a large bite at once.
    "batch_size": 10,
}


def settings(cfg) -> dict:
    out = dict(_DEFAULTS)
    out.update(cfg.raw.get("capture_retry", {}))
    return out


# See the module docstring: NULL must not count as open. Kept as its own
# string (not shared with forward_capture.run_forward's is_open) because that
# one is already deployed and tested; duplicating a well-commented three-line
# fragment is cheaper than coupling two modules over it.
_IS_OPEN = ("(t.closing_date IS NOT NULL AND datetime(t.closing_date) >"
           " datetime('now', '+5 hours', '+30 minutes'))")

_DUE_TENDERS_SQL = f"""
    SELECT t.tender_id, t.detail_url, t.capture_attempts
    FROM tenders t
    WHERE t.detail_url IS NOT NULL
      AND t.next_capture_at IS NOT NULL
      AND datetime(t.next_capture_at) <= datetime('now')
      AND {_IS_OPEN}
      AND (t.status != 'detailed'
           OR EXISTS (SELECT 1 FROM documents d WHERE d.tender_id = t.tender_id
                      AND d.status IN ('pending', 'failed', 'lost')))
    ORDER BY t.next_capture_at ASC
    LIMIT ?
"""


def _is_complete(conn, tender_id: str) -> bool:
    row = conn.execute("SELECT status FROM tenders WHERE tender_id = ?",
                       (tender_id,)).fetchone()
    if row is None or row["status"] != "detailed":
        return False
    outstanding = conn.execute(
        "SELECT 1 FROM documents WHERE tender_id = ? AND status IN"
        " ('pending', 'failed', 'lost') LIMIT 1", (tender_id,)).fetchone()
    return outstanding is None


def run_due_captures(conn, client, cfg, *, limit: int = 10, progress=None) -> dict:
    """Advance every tender currently due for a capture attempt, once each.

    A tender that comes back complete (detailed, nothing pending/failed/lost)
    has its schedule cleared — done, stop asking. One that is still incomplete
    is rescheduled one step further along the progressive backoff. Nothing is
    ever silently dropped: a tender not reached this sweep (the batch cap) is
    simply still due and is first in line next time, since the query orders by
    how overdue it is.
    """
    from .pipeline import fetch_and_store_detail

    _p = progress or (lambda m: log.debug(m))
    rows = conn.execute(_DUE_TENDERS_SQL, (limit,)).fetchall()
    attempted = completed = 0
    for row in rows:
        tender_id = row["tender_id"]
        try:
            fetch_and_store_detail(conn, client, cfg, tender_id, row["detail_url"],
                                   download=True)
        except RequestCapExceeded:
            _p("  capture_retry: request cap reached; stopping sweep")
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("capture retry failed for %s: %s", tender_id, exc)
        attempted += 1
        if _is_complete(conn, tender_id):
            completed += 1
            conn.execute(
                "UPDATE tenders SET capture_attempts=0, next_capture_at=NULL"
                " WHERE tender_id=?", (tender_id,))
        else:
            attempts = (row["capture_attempts"] or 0) + 1
            conn.execute(
                "UPDATE tenders SET capture_attempts=?, next_capture_at=?"
                " WHERE tender_id=?",
                (attempts, next_attempt_after(attempts), tender_id))
        conn.commit()
    if attempted:
        _p(f"  capture_retry: {attempted} due tender(s), {completed} now complete")
    return {"attempted": attempted, "completed": completed}


class CaptureRetryWatch:
    """Wall-clock scheduler for ``run_due_captures``, ticked the same way as
    ``latest_active.LatestWatch`` so the two run on one shared clock without
    either call site needing to know both exist.
    """

    def __init__(self, cfg, *, interval_s: float | None = None, progress=None):
        opts = settings(cfg)
        self.cfg = cfg
        self.opts = opts
        self.interval = float(interval_s if interval_s is not None
                              else opts["check_interval_s"])
        self.progress = progress
        self.totals = {"ticks": 0, "attempted": 0, "completed": 0}
        self._next_due = 0.0
        self._running = False

    def due(self) -> bool:
        return time.monotonic() >= self._next_due

    def tick(self, conn, client, *, force: bool = False) -> dict | None:
        if self._running or not (force or self.due()):
            return None
        self._running = True
        try:
            res = run_due_captures(conn, client, self.cfg,
                                   limit=int(self.opts["batch_size"]),
                                   progress=self.progress)
            self.totals["ticks"] += 1
            self.totals["attempted"] += res["attempted"]
            self.totals["completed"] += res["completed"]
            return res
        except Exception as exc:  # noqa: BLE001
            # Ticked from inside other passes; must never be the reason one dies.
            log.warning("capture retry tick failed: %s", exc)
            return None
        finally:
            self._next_due = time.monotonic() + self.interval
            self._running = False
