"""Procurement red-flag detection and persistence.

Two signals so far, and they are deliberately worded differently, because they
are not equally damning.

**Abnormally short bidding window** (``short_bid_window``) — too little time
between a tender becoming available and its bid-submission close. A few hours
is not a schedule a genuine competitor can answer, so it is a classic indicator
of a pre-arranged ("tailored") bid. This one is an accusation, and the archive
makes it.

**Restricted to invited bidders** (``limited_tender``) — the department used a
Limited tender rather than an open one, so only firms it invited could bid.
This is **not** an accusation, and the wording must never imply that it is:
limited tendering is lawful and often appropriate — small works, emergencies,
proprietary supply. It is flagged because open competition was not used, which
is a fact worth knowing about any contract, and because the method is a
well-known vehicle for favouritism when it is chosen for the wrong reasons.
Whether a given one is legitimate cannot be told from the portal's data, and
this file does not pretend otherwise. Measured on this archive: the median
Limited tender is around Rs 7.9 lakh, but the largest is Rs 11 crore, and 44 of
them *also* carry a short bidding window — the compound cases are where a
reader should start.

Flags are stored in ``redflags`` with the time we first detected them, so each
tender's page can show a persistent warning with the incident details.
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
REASON_LIMITED = "limited_tender"

# The portal's own tender_type values that mean "not open to all comers".
# "Single" (single-source) and "Global Tenders" are deliberately NOT here:
# single-source is a different and rarer thing that deserves its own signal
# rather than being folded into this one, and a global tender is *wider* than
# an open tender, not narrower.
LIMITED_TYPES = ("Limited", "Open Limited", "Closed Limited")


def limited_tender(tender_type: str | None) -> str | None:
    """The tender's restricted-bidding type, or None if it was open to all."""
    tt = (tender_type or "").strip()
    return tt if tt in LIMITED_TYPES else None


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


def check_limited_and_flag(conn, tender_id: str, tender_type: str | None,
                           detected_at: str | None = None) -> bool:
    """Record that a tender was restricted to invited bidders. Idempotent.

    Severity is ``medium`` and stays there. The short-window flag earns ``high``
    when a window is so short that no genuine bid was possible; nothing about a
    Limited tender on its own justifies that, because the method is lawful and
    frequently the right one. A reader is pointed at the compound cases — the
    ones that are *also* short-window — by both flags appearing together, not by
    this one shouting.

    Self-correcting in the same way as the short-window flag: a tender whose
    type is later corrected to an open one has its flag removed rather than
    left standing.
    """
    kind = limited_tender(tender_type)
    if kind is None:
        if tender_type:
            cur = conn.execute("DELETE FROM redflags WHERE tender_id=? AND reason=?",
                               (tender_id, REASON_LIMITED))
            if cur.rowcount:
                log.info("retracted %s for %s: type is %r, open to all bidders",
                         REASON_LIMITED, tender_id, tender_type)
        return False
    detail = (f"Bidding was restricted — the department ran this as a "
              f"“{kind}” tender, so only firms it invited could take part. "
              f"Limited tendering is lawful and often appropriate, but it removes "
              f"open competition, and the portal does not publish the reason.")
    conn.execute(
        """
        INSERT INTO redflags (tender_id, reason, severity, window_hours,
            published_at, closing_at, detail, detected_at)
        VALUES (?, ?, 'medium', NULL, NULL, NULL, ?, ?)
        ON CONFLICT(tender_id, reason) DO UPDATE SET
            severity=excluded.severity, detail=excluded.detail
        """,
        (tender_id, REASON_LIMITED, detail, detected_at or now_iso()),
    )
    return True


def get_flags(conn, tender_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM redflags WHERE tender_id=? ORDER BY detected_at", (tender_id,))]


def scan_all(db_path=None) -> dict:
    """Backfill every signal over the whole archive.

    Uses each tender's ``last_updated_at`` as the detection time, so a
    backfilled flag claims to have been noticed when the archive last saw the
    tender rather than when this scan happened to run.

    The two signals are scanned over different row sets on purpose: a short
    window can only be judged where both dates exist, whereas restricted
    bidding is legible from ``tender_type`` alone and therefore reaches tenders
    that were never detailed.
    """
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

        limited_rows = conn.execute(
            "SELECT tender_id, tender_type, last_updated_at FROM tenders"
            " WHERE tender_type IS NOT NULL"
        ).fetchall()
        limited = 0
        for r in limited_rows:
            if check_limited_and_flag(conn, r["tender_id"], r["tender_type"],
                                      r["last_updated_at"]):
                limited += 1
        conn.commit()

        total = conn.execute("SELECT count(*) FROM redflags").fetchone()[0]
    finally:
        conn.close()
    return {"windows_checked": len(rows), "short_windows_flagged": flagged,
            "types_checked": len(limited_rows), "limited_flagged": limited,
            "total_flags": total}
