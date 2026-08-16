"""The short-window lists on / and /history must agree with the tender page.

These two surfaces regressed *after* the tender page was fixed, and the reason
is worth recording: ``_suspicious_rows`` measured the bidding window in raw SQL
as ``closing_date - published_date``, so it never called ``short_window`` and
the availability fields could not reach it. The tender page said a tender was
clean while the front page and /history went on listing it as rigged — the
archive contradicting itself in public, which is worse than either answer alone.

The rule these tests pin is therefore not "the SQL is correct" but "every
surface reaches the same verdict through the same function".
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tenders.db import init_db
from tenders.redflags import short_window
from tenders.web.dashboard import _suspicious_rows, suspicious_history

# Published Date lands eight days after bidding actually opened — the real
# 2026_MAWS_691071_1 shape. Window by published_date: 3.6h. Real: 8 days.
LATE_PUBLISH = {
    "Published Date": "13-Aug-2026 11:25 AM",
    "Media Publish Date": "01-Aug-2026 09:00 AM",
    "Document Download / Sale Start Date": "05-Aug-2026 09:00 AM",
    "Bid Submission Start Date": "05-Aug-2026 09:00 AM",
    "Bid Submission End Date": "13-Aug-2026 03:00 PM",
}
# Everything agrees it opened the same afternoon: genuinely rigged-looking.
GENUINELY_SHORT = {
    "Published Date": "13-Aug-2026 11:25 AM",
    "Document Download / Sale Start Date": "13-Aug-2026 11:30 AM",
    "Bid Submission Start Date": "13-Aug-2026 11:30 AM",
    "Bid Submission End Date": "13-Aug-2026 03:00 PM",
}
PUBLISHED = "2026-08-13T11:25:00"
CLOSING = "2026-08-13T15:00:00"


@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def add(conn, tid, raw):
    conn.execute(
        "INSERT INTO tenders (tender_id, title, status, source, published_date,"
        " closing_date, raw_json, first_seen_at, last_updated_at)"
        " VALUES (?,?,'detailed','scraped',?,?,?,?,?)",
        (tid, "Borewell work", PUBLISHED, CLOSING, json.dumps(raw),
         "2026-08-13T00:00:00+00:00", "2026-08-13T00:00:00+00:00"))
    conn.commit()


def _ids(rows):
    return [r["tender_id"] for r in rows]


def test_a_late_published_date_is_not_listed_as_suspicious(conn):
    add(conn, "LATE", LATE_PUBLISH)
    assert _ids(_suspicious_rows(conn, "1=1", "hrs ASC", 50)) == []


def test_a_genuinely_short_window_is_still_listed(conn):
    add(conn, "SHORT", GENUINELY_SHORT)
    assert _ids(_suspicious_rows(conn, "1=1", "hrs ASC", 50)) == ["SHORT"]


def test_the_listed_window_is_the_one_the_bidder_actually_had(conn):
    """The hours shown must come from availability, not from published_date."""
    add(conn, "SHORT", GENUINELY_SHORT)
    row = _suspicious_rows(conn, "1=1", "hrs ASC", 50)[0]
    assert row["hrs"] == pytest.approx(
        short_window(PUBLISHED, CLOSING, GENUINELY_SHORT))


def test_every_surface_reaches_the_same_verdict(conn):
    """The regression itself: page and list disagreeing about the same tender."""
    add(conn, "LATE", LATE_PUBLISH)
    add(conn, "SHORT", GENUINELY_SHORT)
    listed = set(_ids(_suspicious_rows(conn, "1=1", "hrs ASC", 50)))
    for tid, raw in (("LATE", LATE_PUBLISH), ("SHORT", GENUINELY_SHORT)):
        page_flags = short_window(PUBLISHED, CLOSING, raw) is not None
        assert (tid in listed) is page_flags, (
            f"{tid}: tender page says flagged={page_flags} but the list "
            f"says {tid in listed} — the archive is contradicting itself")


def test_the_limit_counts_rows_that_survive_the_real_test(conn):
    """A page of 10 must not come back short because the SQL over-selected.

    The SQL pre-filter matches on published_date and hands back rows the real
    test then drops, so a LIMIT applied before that filter silently truncates
    the page. Here every one of the 20 decoys is dropped, and the 5 genuine
    rows must still all be returned.
    """
    for i in range(20):
        add(conn, f"LATE{i}", LATE_PUBLISH)
    for i in range(5):
        add(conn, f"SHORT{i}", GENUINELY_SHORT)
    rows = _suspicious_rows(conn, "1=1", "hrs ASC", 10)
    assert len(rows) == 5
    assert all(r["tender_id"].startswith("SHORT") for r in rows)


def test_history_only_shows_closed_tenders_and_applies_the_same_rule(conn):
    add(conn, "LATE", LATE_PUBLISH)
    add(conn, "SHORT", GENUINELY_SHORT)
    # Both closed in 2026-08; suspicious_history filters on closing_date < now.
    assert _ids(suspicious_history(conn, 50)) == ["SHORT"]
