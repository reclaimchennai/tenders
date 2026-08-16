"""Display formatting for the portal's dates — presentation only.

Nothing here ever feeds a sort, a filter or a comparison against a stored value.
The archive's whole claim is that it holds the record as published, so
``closing_date`` stays the exact ISO string that was scraped; these helpers only
decide how it is spelled on screen.

Two rules the format follows, both learned from misreadings:

* ``13-August-2026`` rather than ``2026-08-13``. The portal writes dates
  day-first, India reads them day-first, and ISO's ``08`` is read as the day by
  roughly half the people who see it. Spelling the month out removes the
  ambiguity entirely rather than swapping one convention for another.
* The relative phrase comes *first*. "Closes in 5 days" is the fact a reader
  wants from a procurement listing; the calendar date is the citation that backs
  it up. Printing only the date makes every reader do the subtraction, and they
  do it against the wrong timezone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..util import parse_date

# The portal's dates (closing_date etc.) are naive IST wall-clock strings
# (see util.parse_date — dateutil with dayfirst=True, no tzinfo attached).
IST = ZoneInfo("Asia/Kolkata")

# Beyond this, days stop being a useful unit — "in 73 days" is arithmetic, not
# information — but below it months are too coarse to plan a bid against.
_DAYS_TO_MONTHS = 60
# Just under a year, so an anniversary reads "1 year ago" and not "12 months".
_DAYS_TO_YEARS = 335
_AVG_MONTH = 30.44


def ist(stamp: str | None) -> datetime | None:
    """Parse a portal date string as IST-aware, or None if unusable."""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt


def fmt_date(stamp: str | None) -> str:
    """``13-August-2026``. Empty string for a missing or unparseable date."""
    dt = ist(stamp)
    if dt is None:
        return ""
    return f"{dt.day:02d}-{dt.strftime('%B')}-{dt.year}"


def fmt_datetime(stamp: str | None) -> str:
    """``13-August-2026 · 17:30``, or just the date if the stamp carries no time.

    A bare ``00:00`` is what the parser produces for a date-only string, not a
    deadline at midnight, so printing it would invent a time the portal never
    published.
    """
    dt = ist(stamp)
    if dt is None:
        return ""
    day = fmt_date(stamp)
    if dt.hour == 0 and dt.minute == 0:
        return day
    return f"{day} · {dt:%H:%M}"


def _elapsed(days: int) -> str:
    """'5 days' / '3 months' / '2 years' for a positive whole-day count."""
    if days < _DAYS_TO_MONTHS:
        return f"{days} days"
    if days < _DAYS_TO_YEARS:
        return f"{round(days / _AVG_MONTH)} months"
    years = round(days / 365.25)
    return f"{years} year{'s' if years != 1 else ''}"


def _distance(stamp: str | None, now: datetime | None) -> tuple[bool, str] | None:
    """(already_passed, 'in 5 days' | '3 months ago' | 'today' | …), or None.

    Whether a deadline is "today" is a question about the calendar in Chennai,
    not about a 24-hour span: a tender closing at 17:30 tonight and one closing
    at 10:00 tomorrow are 16 hours apart but belong in different sentences. The
    difference is therefore taken between IST calendar *dates*, while past
    versus future is decided on the actual instant — so a deadline that passed
    at 09:00 this morning reads "Closed today", never "Closes today".
    """
    closes = ist(stamp)
    if closes is None:
        return None
    now = now or datetime.now(timezone.utc)
    days = (closes.astimezone(IST).date() - now.astimezone(IST).date()).days
    if closes < now:
        if days == 0:
            return True, "today"
        if days == -1:
            return True, "yesterday"
        return True, f"{_elapsed(-days)} ago"
    if days == 0:
        return False, "today"
    if days == 1:
        return False, "tomorrow"
    return False, f"in {_elapsed(days)}"


def relative_close(stamp: str | None, now: datetime | None = None) -> str:
    """'Closes tomorrow' / 'Closes in 5 days' / 'Closed 3 months ago'."""
    got = _distance(stamp, now)
    if got is None:
        return ""
    passed, phrase = got
    return f"{'Closed' if passed else 'Closes'} {phrase}"


def relative_short(stamp: str | None, now: datetime | None = None) -> str:
    """'in 5 days' / '10 days ago' — the same distance with the verb left off.

    For places that state the verb separately. The ticker's status pill already
    says CLOSED, and beside it the full phrase read "CLOSED Closed 10 days ago".
    """
    got = _distance(stamp, now)
    return got[1] if got else ""


def closing_line(stamp: str | None, now: datetime | None = None) -> str:
    """The full 'Closes in 5 days · 13-August-2026' pair used across the site."""
    rel = relative_close(stamp, now)
    if not rel:
        return ""
    return f"{rel} · {fmt_datetime(stamp)}"


# ---------------------------------------------------------------------------
# The critical-date set
# ---------------------------------------------------------------------------

# Stages the portal publishes on a tender's detail page, in the order a reader
# needs them rather than the order the portal prints them: when a bid opens and
# when it shuts is the question people come here with, and the download window
# and the clarification window are context for it.
#
# Each entry is (label, start key, end key). A window is one row with both ends
# on it, because the portal's own two-column layout groups them that way and
# splitting a window across two rows is how "starts 6 Aug" gets read as the
# deadline.
_STAGES: tuple[tuple[str, str, str | None], ...] = (
    ("Published", "Published Date", None),
    ("Bid submission", "Bid Submission Start Date", "Bid Submission End Date"),
    ("Bid opening", "Bid Opening Date", None),
)
_CONTEXT_STAGES: tuple[tuple[str, str, str | None], ...] = (
    ("Document download / sale", "Document Download / Sale Start Date",
     "Document Download / Sale End Date"),
    ("Clarifications", "Clarification Start Date", "Clarification End Date"),
    ("Pre-bid meeting", "Pre Bid Meeting Date", None),
)

# Said on the bid-opening row only when the archive has no award document to
# point at. Bid opening is the day the sealed bids are unsealed; the award is
# published later, often weeks later, and conflating the two would put a wrong
# "result" date on a page people cite.
_OPENING_CAVEAT = ("bids are unsealed on this date — the department publishes "
                   "the result separately")
# The same fact about a tender whose opening date has already passed. A page
# about a tender that closed eight months ago telling the reader that "bids are
# unsealed on this date" reads as a schedule they could still act on; it is a
# record of something that already happened, and it should sound like one.
_OPENING_CAVEAT_PAST = ("bids were unsealed on this date — the department "
                        "publishes the result separately")
# Matches the award panel's own wording, for the same reason it says it there.
_AWARD_CAVEAT = ("the day the department published the award-of-contract "
                 "document, which can be long after bidding closed")


def _portal(raw: object, key: str) -> str | None:
    """One raw_json date as an ISO string, or None if it is unusable.

    The portal writes these two ways — ``31-Jul-2025 04:30 PM`` on a detail page
    and ``30-07-2025 11:45`` on the rows recovered from a status listing — and
    writes the literal string ``NA`` for every stage that does not apply, which
    is most of ``Clarification *`` and ``Pre Bid Meeting Date``. ``parse_date``
    already knows both shapes and already answers None to the NA family, so this
    reuses it rather than growing a second opinion about the portal's dates.
    """
    if not isinstance(raw, dict):
        return None
    return parse_date(raw.get(key))


def critical_dates(tender: dict, now: datetime | None = None) -> list[dict]:
    """The tender's date set, ready to print — never a row we cannot fill.

    Returns ``{label, start, end, note, rel, minor, past}`` dicts, already
    formatted. A stage the portal left as ``NA`` produces no row at all:
    printing "NA" states that a date exists and is called NA, which is worse
    than silence.

    ``past`` says this stage's date has already gone by, and it is computed per
    row rather than once for the tender because the two disagree in the case
    that matters: a live tender's bid-submission window has usually *opened*
    (past) while it has not yet closed (not past). Callers use it to choose a
    tense — "Bid submission ends" against "Bid submission ended" — so that a
    record of something finished stops being written as a schedule.

    Register-only rows (``status='discovered'``, four fifths of the archive)
    carry no ``raw_json``, so they fall through to whatever their columns hold —
    usually nothing, and then this returns ``[]`` and the page shows no block
    rather than an empty one.
    """
    raw = tender.get("raw")
    rows: list[dict] = []

    def gone(stamp: str | None) -> bool:
        """Has this moment already passed, in Chennai wall-clock terms."""
        got = _distance(stamp, now)
        return bool(got and got[0])

    def add(label: str, start: str | None, end: str | None, *,
            note: str = "", rel: str = "", minor: bool = False,
            day_only: bool = False) -> None:
        show = fmt_date if day_only else fmt_datetime
        shown_start, shown_end = show(start), show(end)
        if not shown_start and not shown_end:
            return
        # Judged on the date the row actually leads with — the deadline for a
        # window, the single date otherwise — because that is the one the label
        # is a statement about.
        rows.append({"label": label, "start": shown_start, "end": shown_end,
                     "note": note, "rel": rel, "minor": minor,
                     "past": gone(end or start)})

    for label, start_key, end_key in _STAGES:
        start = _portal(raw, start_key)
        end = _portal(raw, end_key) if end_key else None
        # The two columns the scraper normalised are the same two facts, and
        # they are present on rows that never got a detail page.
        if label == "Published":
            start = start or tender.get("published_date")
        elif label == "Bid submission":
            end = end or tender.get("closing_date")
        note = ""
        if label == "Bid opening" and not tender.get("awarded_at"):
            note = _OPENING_CAVEAT_PAST if gone(start) else _OPENING_CAVEAT
        add(label, start, end, note=note,
            rel=relative_close(end, now) if label == "Bid submission" else "")

    # The archive's own AOC date, not the portal's bid-opening date: this is the
    # day the award was actually published, and it is the only honest answer to
    # "when was the result announced".
    #
    # To the day, matching the award panel. The stamp underneath it is the
    # minute the officer's digital signature was applied, which is a fact about
    # an afternoon in an office and not a published time — printing "· 19:02"
    # beside it would give it the same weight as a bid deadline.
    add("Result announced", tender.get("awarded_at"), None,
        note=_AWARD_CAVEAT, day_only=True)

    # A context window that is character-for-character the bid window says
    # nothing the row above it did not, and departments copy the bid dates into
    # the download window constantly. Printing it anyway would make the two
    # cases — "you may download for exactly as long as you may bid" and "the
    # documents came down early" — look identical, when only the second is worth
    # a reader's attention.
    bid = (_portal(raw, "Bid Submission Start Date"),
           _portal(raw, "Bid Submission End Date") or tender.get("closing_date"))
    for label, start_key, end_key in _CONTEXT_STAGES:
        start = _portal(raw, start_key)
        end = _portal(raw, end_key) if end_key else None
        if end is not None and (start, end) == bid:
            continue
        add(label, start, end, minor=True)
    return rows
