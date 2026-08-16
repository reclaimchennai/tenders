"""What the critical-date block is allowed to say, and what it must not.

The archive's whole claim is that it holds the record as published, and this is
the one place that *promotes* a handful of the portal's dates over the rest. A
promotion that mislabels one of them is worse than the buried table it replaced,
so the tests here are written as the two specific misstatements available:
calling a bid opening a result, and printing the portal's literal "NA" as though
it were a date.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from tenders.web.dates import critical_dates, fmt_date, fmt_datetime

# Both shapes the portal writes. The first is a detail page; the second is what
# the status-listing rows carry, and it is dd-mm-yyyy — read the other way round
# it is a different month.
DETAIL = {
    "Published Date": "31-Jul-2025 04:30 PM",
    "Bid Submission Start Date": "06-Aug-2025 10:00 AM",
    "Bid Submission End Date": "18-Aug-2025 03:00 PM",
    "Bid Opening Date": "19-Aug-2025 03:30 PM",
    "Document Download / Sale Start Date": "31-Jul-2025 04:30 PM",
    "Document Download / Sale End Date": "18-Aug-2025 03:00 PM",
    "Clarification Start Date": "NA",
    "Clarification End Date": "NA",
    "Pre Bid Meeting Date": "",
}
LISTING = {
    "Published Date": "30-07-2025 11:45",
    "Bid Submission Start Date": "30-07-2025 11:45",
    "Bid Submission End Date": "01-08-2025 15:00",
    "Bid Opening Date": "01-08-2025 15:30",
}

NOW = datetime.fromisoformat("2025-08-10T12:00:00+05:30")


def rows(tender, now=NOW):
    return {r["label"]: r for r in critical_dates(tender, now)}


# ---------------------------------------------------------------------------
# Never print "NA"
# ---------------------------------------------------------------------------

def test_a_stage_the_portal_marked_NA_produces_no_row():
    """'NA' is the portal saying the stage does not apply, not a date.

    It had already been caught being echoed back as "Value NA" and "NA KB". A
    table of dates is the easiest place for it to happen again, because most
    tenders have no clarification window and no pre-bid meeting at all.
    """
    got = rows({"raw": DETAIL})
    assert "Clarifications" not in got
    assert "Pre-bid meeting" not in got
    assert not any("NA" in r["start"] + r["end"] for r in got.values())


@pytest.mark.parametrize("value", ["NA", "N/A", "Nil", "-", "", "   ", None])
def test_the_whole_not_applicable_family_is_suppressed(value):
    got = rows({"raw": dict(DETAIL, **{"Bid Opening Date": value})})
    assert "Bid opening" not in got


def test_a_register_only_tender_renders_nothing_at_all():
    """Four fifths of the archive has no raw_json. It must degrade to no block,
    not to a block of empty rows."""
    assert critical_dates({"raw": {}, "tender_id": "X"}) == []
    assert critical_dates({}) == []
    assert critical_dates({"raw": None}) == []


# ---------------------------------------------------------------------------
# Never call a bid opening a result
# ---------------------------------------------------------------------------

def test_the_result_row_comes_from_the_award_date_and_says_so():
    got = rows({"raw": DETAIL, "awarded_at": "2025-09-14T19:02:50"})
    assert got["Result announced"]["start"] == "14-September-2025"
    assert "award-of-contract" in got["Result announced"]["note"]
    # And the award date is stated to the day: the timestamp under it is when an
    # officer's signature was applied, not a published time.
    assert "·" not in got["Result announced"]["start"]


def test_without_an_award_there_is_no_result_row_and_bid_opening_says_why():
    """The fallback is labelled as what it is. Bid opening is the day the sealed
    bids are unsealed; the award is published separately, often weeks later, and
    an archive people cite must not conflate the two."""
    got = rows({"raw": DETAIL})
    assert "Result announced" not in got
    assert got["Bid opening"]["start"] == "19-August-2025 · 15:30"
    assert "result separately" in got["Bid opening"]["note"]


def test_an_awarded_tender_stops_hedging_about_bid_opening():
    got = rows({"raw": DETAIL, "awarded_at": "2025-09-14T19:02:50"})
    assert got["Bid opening"]["note"] == ""


# ---------------------------------------------------------------------------
# The set itself
# ---------------------------------------------------------------------------

def test_the_live_tender_set_is_present_and_first():
    """Published, bid submission open, bid submission close — the three the
    block exists to stop burying."""
    got = critical_dates({"raw": DETAIL}, NOW)
    assert [r["label"] for r in got][:3] == [
        "Published", "Bid submission", "Bid opening"]
    by = {r["label"]: r for r in got}
    assert by["Published"]["start"] == "31-July-2025 · 16:30"
    assert by["Bid submission"]["start"] == "06-August-2025 · 10:00"
    assert by["Bid submission"]["end"] == "18-August-2025 · 15:00"


def test_the_relative_phrase_rides_on_the_closing_end_only():
    got = rows({"raw": DETAIL})
    assert got["Bid submission"]["rel"] == "Closes in 8 days"
    assert got["Published"]["rel"] == ""


def test_the_listing_format_is_read_day_first():
    """01-08-2025 is the first of August, not the eighth of January."""
    got = rows({"raw": LISTING})
    assert got["Bid submission"]["end"] == "01-August-2025 · 15:00"
    assert got["Published"]["start"] == "30-July-2025 · 11:45"


def test_the_normalised_columns_stand_in_when_raw_json_is_missing():
    """A tender that never got a detail page can still carry these two."""
    got = rows({"raw": {}, "published_date": "2025-07-30T11:45:00",
                "closing_date": "2025-08-01T15:00:00"})
    assert got["Published"]["start"] == "30-July-2025 · 11:45"
    assert got["Bid submission"]["end"] == "01-August-2025 · 15:00"
    assert got["Bid submission"]["start"] == ""


def test_download_and_clarification_windows_are_marked_secondary():
    got = rows({"raw": DETAIL})
    assert got["Document download / sale"]["minor"] is True
    assert got["Bid submission"]["minor"] is False


# ---------------------------------------------------------------------------
# One formatter
# ---------------------------------------------------------------------------

def test_the_block_spells_dates_the_way_the_rest_of_the_site_does():
    """DD-Mmmm-YYYY, from the same helpers, so a second convention cannot drift
    into existence beside the first."""
    got = rows({"raw": DETAIL, "awarded_at": "2025-09-14T19:02:50"})
    assert got["Published"]["start"] == fmt_datetime("2025-07-31T16:30:00")
    assert got["Result announced"]["start"] == fmt_date("2025-09-14T19:02:50")


def test_a_context_window_identical_to_the_bid_window_is_not_repeated():
    """Departments copy the bid dates into the download window constantly, and
    the duplicate row makes the informative case — documents withdrawn early —
    look exactly like the ordinary one."""
    same = dict(DETAIL,
                **{"Document Download / Sale Start Date": "06-Aug-2025 10:00 AM",
                   "Document Download / Sale End Date": "18-Aug-2025 03:00 PM"})
    assert "Document download / sale" not in rows({"raw": same})
    # One hour earlier is a different fact, so it survives.
    early = dict(same, **{"Document Download / Sale End Date": "18-Aug-2025 02:00 PM"})
    assert "Document download / sale" in rows({"raw": early})
