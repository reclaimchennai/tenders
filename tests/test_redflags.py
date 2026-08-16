"""The short-bidding-window flag must not accuse a department wrongly.

This archive's only asset is that its accusations survive scrutiny. A tender
page that says "no genuine bidder can prepare in that time" about a tender that
was advertised twelve days earlier does more damage than a missed flag: it is
the argument an accused department needs to dismiss the whole mirror.

The regression these tests pin is real. 2026_MAWS_691071_1 carried a portal
"Published Date" of 13-Aug-2026 11:25 against a bid window that opened 05-Aug
and a newspaper advertisement dated 01-Aug, and the site published "Only 3.6
hours between publication and bid closing" — contradicted by the tender's own
stored fields.
"""

from __future__ import annotations

import sqlite3

import pytest

from tenders.db import init_db
from tenders.redflags import REASON, available_from, check_and_flag, short_window


# The real tender, as stored.
LATE_PUBLISH_RAW = {
    "Published Date": "13-Aug-2026 11:25 AM",
    "Media Publish Date": "01-Aug-2026 09:00 AM",
    "Document Download / Sale Start Date": "05-Aug-2026 09:00 AM",
    "Bid Submission Start Date": "05-Aug-2026 09:00 AM",
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


def _add(conn, tid="T1"):
    conn.execute(
        "INSERT INTO tenders (tender_id, title, status, source,"
        " first_seen_at, last_updated_at) VALUES (?,'T','detailed','scraped',"
        " '2026-08-13T00:00:00+00:00','2026-08-13T00:00:00+00:00')", (tid,))
    conn.commit()


def _flags(conn, tid="T1"):
    return conn.execute("SELECT count(*) FROM redflags WHERE tender_id=? AND reason=?",
                        (tid, REASON)).fetchone()[0]


def test_availability_is_taken_from_the_earliest_proving_field():
    assert available_from(PUBLISHED, LATE_PUBLISH_RAW) == "2026-08-01T09:00:00"


def test_a_late_published_date_no_longer_manufactures_a_red_flag():
    # Published Date alone says 3.6 hours...
    assert short_window(PUBLISHED, CLOSING) == pytest.approx(3.58, abs=0.1)
    # ...but the tender's own fields prove bidders had eight days.
    assert short_window(PUBLISHED, CLOSING, LATE_PUBLISH_RAW) is None


def test_a_genuinely_short_window_is_still_flagged():
    """Every availability field agrees it opened the same afternoon."""
    raw = {
        "Published Date": "13-Aug-2026 11:25 AM",
        "Media Publish Date": "NA",
        "Document Download / Sale Start Date": "13-Aug-2026 11:30 AM",
        "Bid Submission Start Date": "13-Aug-2026 11:30 AM",
    }
    assert short_window(PUBLISHED, CLOSING, raw) == pytest.approx(3.58, abs=0.1)


def test_missing_and_NA_fields_never_widen_the_window():
    """A tender with no availability evidence keeps the Published Date verdict.

    Absence of evidence must not clear a flag — only contradicting evidence may.
    """
    assert short_window(PUBLISHED, CLOSING, {}) == pytest.approx(3.58, abs=0.1)
    assert short_window(PUBLISHED, CLOSING, {"Bid Submission Start Date": "NA"}) \
        == pytest.approx(3.58, abs=0.1)


def test_detail_page_retracts_a_provisional_flag_raised_from_a_listing(conn):
    """The listing poll flags on partial data; the detail page must correct it."""
    _add(conn)
    # Listing row: only published + closing are known, so it flags.
    assert check_and_flag(conn, "T1", PUBLISHED, CLOSING) is True
    assert _flags(conn) == 1

    # Detail page arrives carrying the bid-submission dates, disproving it.
    assert check_and_flag(conn, "T1", PUBLISHED, CLOSING, raw=LATE_PUBLISH_RAW) is False
    assert _flags(conn) == 0, "a disproven flag must be retracted, not left standing"


def test_a_listing_only_recheck_does_not_clear_a_confirmed_flag(conn):
    """Absence of raw data is not evidence; the poll must not undo the detail pass."""
    _add(conn)
    check_and_flag(conn, "T1", PUBLISHED, CLOSING)
    assert _flags(conn) == 1
    # The newest-first poll re-sees this tender every few minutes with no raw.
    check_and_flag(conn, "T1", PUBLISHED, CLOSING)
    assert _flags(conn) == 1
