"""Procurement red-flag detection and persistence.

The first implemented signal is an **abnormally short bidding window**: too
little time between a tender's publication and its bid-submission close. A short
window is a classic indicator of a pre-arranged ("tailored") bid — genuine
bidders can't realistically prepare in a few hours. Flags are stored in the
``redflags`` table with the time we first detected them, so each tender's page
can show a persistent warning with the incident details.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from .config import load_config
from .db import connect, init_db
from .util import now_iso, parse_date

log = logging.getLogger("redflags")

# A bid window below this is suspicious; below HIGH it is egregious.
THRESHOLD_HOURS = 24.0
HIGH_HOURS = 6.0
REASON = "short_bid_window"


def _window_hours(published: str | None, closing: str | None) -> float | None:
    if not published or not closing:
        return None
    try:
        p = datetime.fromisoformat(published)
        c = datetime.fromisoformat(closing)
    except ValueError:
        return None
    return (c - p).total_seconds() / 3600.0


# Detail-page fields that each independently prove the tender was already
# public and actionable at that moment. Any one of them predating the portal's
# own "Published Date" means a bidder had longer than that date suggests.
_AVAILABILITY_FIELDS = (
    "Bid Submission Start Date",             # bids were accepted from here
    "Document Download / Sale Start Date",   # the papers could be obtained here
    "Media Publish Date",                    # it was advertised from here
)


def available_from(published: str | None, raw: dict | None) -> str | None:
    """Earliest moment this tender is *proven* to have been public.

    The portal's own "Published Date" is not that moment. It is an e-publishing
    timestamp and departments routinely enter it late: 2026_MAWS_691071_1
    carries Published Date 13-Aug-2026 11:25 against a bid submission window
    that opened 05-Aug and a newspaper advertisement dated 01-Aug. Measured
    from Published Date alone the archive announced "only 3.6 hours between
    publication and bid closing"; bidders in fact had eight days, and the page
    was making a corruption accusation that the tender's own stored fields
    disproved.

    A false red flag is the most expensive bug this project can ship — the
    archive is worth something only while its accusations hold up. So the
    window is measured from the earliest field that independently proves
    availability, and a tender is called short only when no such evidence
    contradicts it.
    """
    best = published
    for key in _AVAILABILITY_FIELDS:
        value = (raw or {}).get(key)
        if not value or str(value).strip().upper() == "NA":
            continue
        parsed = parse_date(str(value))
        if parsed and (best is None or parsed < best):
            best = parsed
    return best


def short_window(published: str | None, closing: str | None,
                 raw: dict | None = None) -> float | None:
    """Bid-window length in hours if it is suspiciously short, else None.

    Derived live from the tender's own dates so the warning shows even for
    tenders scraped before the ``redflags`` backfill last ran. ``raw`` is the
    tender's parsed ``raw_json`` once the detail page has been read; without it
    this falls back to Published Date, which is all a listing row carries. See
    ``check_and_flag`` for how a provisional flag raised from a listing is
    retracted when the detail page later disproves it.
    """
    hrs = _window_hours(available_from(published, raw), closing)
    if hrs is None or hrs <= 0 or hrs >= THRESHOLD_HOURS:
        return None
    return hrs


def check_and_flag(conn, tender_id: str, published: str | None, closing: str | None,
                   detected_at: str | None = None, raw: dict | None = None) -> bool:
    """Flag a tender if its bidding window is suspiciously short. Idempotent:
    detected_at is preserved on the first detection.

    Self-correcting. The fast listing poll calls this with only Published Date
    and closing date, because that is all a listing row carries, so a flag
    raised there is provisional. When the detail page is later read it brings
    the bid-submission and advertisement dates with it (see ``available_from``)
    and can prove the window was never short — at which point the stored flag
    is **deleted**, not left standing. An archive that keeps publishing an
    accusation it can already disprove is worse than one that never made it.
    """
    hrs = short_window(published, closing, raw)
    if hrs is None:
        # Only retract on evidence: `raw` present means a detail page was read
        # and actively disagreed. A listing-only call that simply lacks the
        # dates must not clear a flag some earlier detail pass established.
        if raw is not None:
            cur = conn.execute("DELETE FROM redflags WHERE tender_id=? AND reason=?",
                               (tender_id, REASON))
            if cur.rowcount:
                log.info("retracted %s for %s: bid window was not short after all",
                         REASON, tender_id)
        return False
    severity = "high" if hrs < HIGH_HOURS else "medium"
    detail = (f"Only {hrs:.1f} hours between the tender becoming available and "
              f"bid-closing — well under the {int(THRESHOLD_HOURS)}-hour norm.")
    conn.execute(
        """
        INSERT INTO redflags (tender_id, reason, severity, window_hours,
            published_at, closing_at, detail, detected_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tender_id, reason) DO UPDATE SET
            severity=excluded.severity, window_hours=excluded.window_hours,
            published_at=excluded.published_at, closing_at=excluded.closing_at,
            detail=excluded.detail
        """,
        (tender_id, REASON, severity, round(hrs, 2), published, closing, detail,
         detected_at or now_iso()),
    )
    return True


def get_flags(conn, tender_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM redflags WHERE tender_id=? ORDER BY detected_at", (tender_id,))]


def scan_all(db_path=None) -> dict:
    """Backfill: flag every tender with a short bidding window. Uses the tender's
    last_updated_at as the detection time (when we last saw it)."""
    cfg = load_config()
    db_path = db_path or cfg.db_path
    init_db(db_path)
    conn = connect(db_path)
    flagged = 0
    try:
        rows = conn.execute(
            "SELECT tender_id, published_date, closing_date, last_updated_at,"
            " raw_json FROM tenders"
            " WHERE published_date IS NOT NULL AND closing_date IS NOT NULL"
        ).fetchall()
        for r in rows:
            try:
                raw = json.loads(r["raw_json"] or "{}")
            except (TypeError, ValueError):
                raw = {}
            if check_and_flag(conn, r["tender_id"], r["published_date"],
                              r["closing_date"], r["last_updated_at"],
                              raw=raw if isinstance(raw, dict) else {}):
                flagged += 1
        conn.commit()
        total = conn.execute("SELECT count(*) FROM redflags").fetchone()[0]
    finally:
        conn.close()
    return {"newly_checked": len(rows), "flagged_now": flagged, "total_flags": total}
