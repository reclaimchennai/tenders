"""Document capture state machine: retry scheduling and its instrumentation.

Two concerns live here because they are the same question seen from two sides.

**Scheduling.** A tender whose documents are still missing is re-probed while it
is open, and never once it has closed (closed tenders' files are genuinely
deleted; re-fetching them only spends a public portal's bandwidth). The probe
is front-loaded — 1, 5, 15, 30 minutes, then hourly — because a document link
is most likely to appear soon after a tender is published or first noticed
missing, and *bounded*: a document that has been missing for two days is
almost certainly never going to appear, so it drops to a daily re-probe
instead of being hammered forever.

**Instrumentation.** The interval above is a guess. The question it should be
answering — *how long after a tender is published do its documents actually land
on the portal, and at what time of day* — is unanswerable from the data the
schema kept, because ``documents.downloaded_at`` records when **we** fetched a
file, which during a backfill is just our own catch-up schedule. So every state
transition that bears on it is now appended to ``document_events``:

* ``seen_missing``   — a detail fetch showed the document with no download link
* ``link_appeared``  — a later detail fetch showed a link for it
* ``captured`` / ``download_failed`` / ``version_captured`` / ``integrity_failed``

Once a few days of *live* observations have accumulated, ``lag_report`` yields
the real distribution and ``next_attempt_after`` can be driven by it (probe hard
inside the window where documents actually land, back off outside it). That
tuning is deliberately **not** attempted yet: there is no honest data for it.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from .util import now_iso

log = logging.getLogger("doclife")

# Probe cadence for a still-open tender whose documents are missing, keyed by
# how many consecutive attempts have already come back incomplete. Front-loaded
# on purpose: a document link is most likely to appear in the minutes right
# after a tender is published (or a probe first notices it is missing), so the
# early retries are close together and back off once that window has passed.
# Index 0 is the wait before the *first* retry, index 1 before the second, and
# so on; past the end of the list the cadence flattens to RETRY_INTERVAL_S.
#
# This is also the schedule new tenders are re-probed on before they have even
# been detailed once (see capture_retry.py) — "get the documents" and "get the
# detail page that lists them" are the same race against the portal's deletion
# clock, so both use one policy rather than two that could drift apart.
RETRY_SCHEDULE_S = [60, 300, 900, 1800]  # 1m, 5m, 15m, 30m
RETRY_INTERVAL_S = 3600  # steady state: every 60m after the schedule above

# After this many consecutive failed probes (~2 days, given the schedule above)
# a document is treated as permanently unavailable rather than late, and drops
# to a slow re-probe. It is never abandoned outright: the portal does
# occasionally re-publish, and a once-a-day HEAD-weight check costs nothing
# measurable.
MAX_HOURLY_ATTEMPTS = 48
SLOW_RETRY_INTERVAL_S = 86_400

# How long a captured document's bytes are trusted before the modification pass
# considers re-downloading them to compare hashes.
RECHECK_INTERVAL_S = 12 * 3600

IST_OFFSET = timedelta(hours=5, minutes=30)


def next_attempt_after(attempts: int, *, now: datetime | None = None) -> str:
    """When something with ``attempts`` consecutive incomplete probes may be
    tried again.

    Single source of truth for the cadence, so retuning it (see module
    docstring) — or eventually replacing the flat tail with a measured one — is
    a change to this function alone. ``attempts`` is 1 for the wait before the
    first retry, matching how documents.attempts and tenders.capture_attempts
    are incremented (see note_missing/note_download_failed and
    capture_retry.run_due_captures).
    """
    if 1 <= attempts <= len(RETRY_SCHEDULE_S):
        interval = RETRY_SCHEDULE_S[attempts - 1]
    elif attempts < MAX_HOURLY_ATTEMPTS:
        interval = RETRY_INTERVAL_S
    else:
        interval = SLOW_RETRY_INTERVAL_S
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(seconds=interval)).replace(microsecond=0).isoformat()


def next_recheck_after(*, now: datetime | None = None) -> str:
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(seconds=RECHECK_INTERVAL_S)).replace(
        microsecond=0).isoformat()


def record_event(conn: sqlite3.Connection, document_id: int | None,
                 tender_id: str | None, event: str, detail: str | None = None,
                 at: str | None = None) -> None:
    """Append one lifecycle transition. Never raises — instrumentation must not
    be able to abort a capture."""
    try:
        conn.execute(
            "INSERT INTO document_events (document_id, tender_id, event, detail, at)"
            " VALUES (?, ?, ?, ?, ?)",
            (document_id, tender_id, event, detail, at or now_iso()),
        )
    except sqlite3.Error as exc:
        log.warning("could not record %s event for doc %s: %s", event,
                    document_id, exc)


def note_missing(conn, doc_id: int, tender_id: str, prev_missing_at: str | None,
                 attempts: int, *, first_time: bool) -> None:
    """Record a probe that found the document still without a download link."""
    now = now_iso()
    conn.execute(
        "UPDATE documents SET attempts = attempts + 1, last_attempt_at = ?,"
        " next_attempt_at = ?, first_seen_missing_at = COALESCE(first_seen_missing_at, ?)"
        " WHERE id = ?",
        (now, next_attempt_after(attempts + 1), now, doc_id),
    )
    if first_time or prev_missing_at is None:
        record_event(conn, doc_id, tender_id, "seen_missing", at=now)


def note_link_appeared(conn, doc_id: int, tender_id: str) -> None:
    """A download link exists where none did before: reset the retry budget.

    The attempt counter measures *consecutive* failures, so a link reappearing
    must clear it — otherwise a document that went missing for two days and came
    back would inherit the slow schedule and be captured late or not at all.
    """
    now = now_iso()
    conn.execute(
        "UPDATE documents SET attempts = 0, next_attempt_at = NULL,"
        " link_first_seen_at = COALESCE(link_first_seen_at, ?) WHERE id = ?",
        (now, doc_id),
    )
    record_event(conn, doc_id, tender_id, "link_appeared", at=now)


def note_download_failed(conn, doc_id: int, tender_id: str, attempts: int,
                         reason: str) -> None:
    now = now_iso()
    conn.execute(
        "UPDATE documents SET status='failed', attempts = attempts + 1,"
        " last_attempt_at = ?, next_attempt_at = ? WHERE id = ?",
        (now, next_attempt_after(attempts + 1), doc_id),
    )
    record_event(conn, doc_id, tender_id, "download_failed", reason, at=now)


def due_clause(alias: str = "d") -> str:
    """SQL predicate: this document row is due for another capture attempt."""
    return (f"({alias}.next_attempt_at IS NULL"
            f" OR datetime({alias}.next_attempt_at) <= datetime('now'))")


# ---------------------------------------------------------------------------
# Reporting


def _percentiles(values: list[float], points=(10, 25, 50, 75, 90)) -> dict:
    if not values:
        return {}
    s = sorted(values)
    out = {"n": len(s), "min": round(s[0], 2), "max": round(s[-1], 2)}
    for p in points:
        idx = min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))
        out[f"p{p}"] = round(s[idx], 2)
    return out


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def lag_report(conn: sqlite3.Connection) -> dict:
    """How long documents take to appear, measured from observed transitions only.

    Deliberately ignores ``downloaded_at``: during a backfill it reflects our own
    catch-up order, not the portal. Only documents we personally watched go from
    "no link" to "link" contribute, so the report is empty until the instrumented
    scraper has been running for a while — that emptiness is the honest answer.
    """
    rows = conn.execute(
        "SELECT d.id, d.tender_id, d.first_seen_missing_at, d.link_first_seen_at,"
        "       t.published_date"
        " FROM documents d LEFT JOIN tenders t ON t.tender_id = d.tender_id"
        " WHERE d.link_first_seen_at IS NOT NULL"
    ).fetchall()

    from_missing: list[float] = []
    from_publish: list[float] = []
    hour_hist = {h: 0 for h in range(24)}
    for r in rows:
        appeared = _parse(r["link_first_seen_at"])
        if appeared is None:
            continue
        hour_hist[(appeared + IST_OFFSET).hour] += 1
        missing = _parse(r["first_seen_missing_at"])
        if missing is not None and appeared >= missing:
            from_missing.append((appeared - missing).total_seconds() / 3600)
        published = _parse(r["published_date"])
        if published is not None:
            from_publish.append((appeared - published).total_seconds() / 3600)

    watched = conn.execute(
        "SELECT count(*) FROM documents WHERE first_seen_missing_at IS NOT NULL"
    ).fetchone()[0]
    still_missing = conn.execute(
        "SELECT count(*) FROM documents WHERE first_seen_missing_at IS NOT NULL"
        " AND link_first_seen_at IS NULL"
    ).fetchone()[0]

    return {
        "observations": len(rows),
        "documents_watched_missing": watched,
        "still_missing": still_missing,
        # Hours from us first seeing the document listed with no link, to a link
        # appearing. This is the number the retry schedule should be tuned to.
        "hours_missing_to_link": _percentiles(from_missing),
        # Hours from the tender's own published date (portal clock, naive IST)
        # to the link appearing; noisier but comparable across tenders.
        "hours_published_to_link": _percentiles(from_publish),
        "link_appeared_by_ist_hour": hour_hist,
        "note": ("Empty or tiny samples mean the instrumentation has not yet "
                 "observed enough live transitions; the retry interval remains "
                 "a fixed hourly guess until it has."),
    }
