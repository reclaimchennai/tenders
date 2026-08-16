"""Search-layer guards.

These cover the two ways this layer can be wrong without looking wrong: a query
the user is allowed to type that the FTS parser cannot compile, and an index
whose predicate has drifted away from the WHERE term it was built for.
"""

from __future__ import annotations

import sqlite3

import pytest

from tenders.db import CRITERIA_FLAGS, init_db
from tenders.index_fts import ensure_docs_fts_aligned
from tenders.web.search import (
    CRITERIA,
    build_match,
    search_documents,
    search_tenders_advanced,
)


@pytest.fixture()
def conn(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.execute(
        "INSERT INTO tenders (tender_id, title, work_description, organisation_chain,"
        " location, closing_date, published_date, tender_category, tender_value_num,"
        " raw_json, source, status, first_seen_at, last_updated_at)"
        " VALUES ('T1','Road widening AND drain','storm water drain work',"
        " 'Highways Department||Chennai','Chennai','2025-06-01T15:00:00',"
        " '2025-05-01T10:00:00','Works',500000.0,"
        """ '{"EMD Exemption Allowed":"Yes","Form Of Contract":"Works"}',"""
        " 'scraped','detailed','2025-05-01','2025-05-01')"
    )
    # Same closing_date as T1: the pair is what makes the tiebreaker observable.
    c.execute(
        "INSERT INTO tenders (tender_id, title, organisation_chain, closing_date,"
        " published_date, source, status, first_seen_at, last_updated_at)"
        " VALUES ('T2','Bridge repair','Highways Department||Salem',"
        " '2025-06-01T15:00:00','2025-05-02T10:00:00','scraped','detailed',"
        " '2025-05-02','2025-05-02')"
    )
    c.execute(
        "INSERT INTO tenders (tender_id, title, organisation_chain, closing_date,"
        " published_date, source, status, first_seen_at, last_updated_at)"
        " VALUES ('T3','Pump house','Rural Development','2025-07-01T15:00:00',"
        " '2025-05-03T10:00:00','scraped','detailed','2025-05-03','2025-05-03')"
    )
    c.execute(
        "INSERT INTO tenders_fts (tender_id, title, work_description,"
        " organisation_chain, location, reference_number)"
        " SELECT tender_id, title, work_description, organisation_chain, location,"
        " reference_number FROM tenders"
    )
    c.commit()
    yield c
    c.close()


@pytest.mark.parametrize(
    "query",
    ["road", "road AND bridge", "water OR tank", "road NOT bridge", "NEAR", "AND",
     "OR", "NOT", '"storm water" drain', "road-work", "a", "(pump)", '"', "* *",
     "AND OR NOT NEAR"],
)
def test_every_query_compiles(conn, query):
    """Nothing a user can type into the box may reach FTS5 as invalid syntax.

    AND/OR/NOT/NEAR are FTS5 operators as barewords, so before they were quoted
    a search for "road AND bridge" was a 500.
    """
    match = build_match(query)
    if match is None:
        return
    search_tenders_advanced(conn, match)
    search_documents(conn, match, 25, 0)


def test_bare_words_are_prefix_matched(conn):
    rows, total = search_tenders_advanced(conn, build_match("roa"))
    assert total == 1 and rows[0]["tender_id"] == "T1"


def test_operator_words_are_searched_literally(conn):
    """'AND' is in T1's title, so it must find T1 rather than act as an operator."""
    rows, _ = search_tenders_advanced(conn, build_match("widening AND drain"))
    assert [r["tender_id"] for r in rows] == ["T1"]


def test_default_order_is_total(conn):
    """Ties on closing_date must still have one defined order, or paging leaks."""
    rows, total = search_tenders_advanced(conn, None)
    assert total == 3
    assert [r["tender_id"] for r in rows] == ["T3", "T2", "T1"]
    first, _ = search_tenders_advanced(conn, None, limit=2, offset=0)
    second, _ = search_tenders_advanced(conn, None, limit=2, offset=2)
    assert [r["tender_id"] for r in first + second] == ["T3", "T2", "T1"]


def test_filtered_and_unfiltered_orders_agree(conn):
    """The filtered path forces a sort; it must still produce the same sequence."""
    unfiltered, _ = search_tenders_advanced(conn, None)
    filtered, _ = search_tenders_advanced(conn, None, value_min="")
    assert [r["tender_id"] for r in unfiltered] == [r["tender_id"] for r in filtered]
    org, _ = search_tenders_advanced(conn, None, org="Highways Department")
    assert [r["tender_id"] for r in org] == ["T2", "T1"]


def _typed(conn):
    """Give the three fixture tenders a type and a second category to filter on."""
    conn.execute("UPDATE tenders SET tender_type='Open Tender' WHERE tender_id<>'T2'")
    conn.execute("UPDATE tenders SET tender_type='Limited' WHERE tender_id='T2'")
    conn.execute("UPDATE tenders SET tender_category='Goods' WHERE tender_id='T3'")
    return conn


def _ids(result):
    return sorted(r["tender_id"] for r in result[0])


def test_a_filter_ors_within_itself_and_ands_across_fields(conn):
    """Two chips in one field widen the search; two fields narrow it.

    This is the whole contract of the tag-chip filters, and it is the half of it
    that is easy to get backwards: ANDing within a field asks for a tender that
    is in two departments at once, which is always nothing.
    """
    _typed(conn)
    one = search_tenders_advanced(conn, None, org=["Highways Department"])
    assert _ids(one) == ["T1", "T2"]
    both = search_tenders_advanced(
        conn, None, org=["Highways Department", "Rural Development"])
    assert _ids(both) == ["T1", "T2", "T3"]
    assert _ids(search_tenders_advanced(conn, None, category=["Works", "Goods"])) \
        == ["T1", "T3"]
    assert _ids(search_tenders_advanced(conn, None,
                                        tender_type=["Open Tender", "Limited"])) \
        == ["T1", "T2", "T3"]
    # Across fields: either department AND that one category.
    assert _ids(search_tenders_advanced(
        conn, None, org=["Highways Department", "Rural Development"],
        category=["Goods"])) == ["T3"]
    # A combination nothing satisfies is empty, not everything.
    assert _ids(search_tenders_advanced(
        conn, None, org=["Rural Development"], category=["Works"])) == []


def test_single_value_links_still_mean_what_they_meant(conn):
    """Every org chip, share link and bookmark in the wild passes one string."""
    _typed(conn)
    for field, value in (("org", "Highways Department"),
                         ("category", "Works"),
                         ("tender_type", "Limited")):
        as_string = search_tenders_advanced(conn, None, **{field: value})
        as_list = search_tenders_advanced(conn, None, **{field: [value]})
        assert _ids(as_string) == _ids(as_list) and as_string[1] == as_list[1]


def test_blank_choices_are_dropped_not_matched(conn):
    """"Any" submits an empty value; filtering on it would match nothing at all."""
    _typed(conn)
    assert _ids(search_tenders_advanced(conn, None, org=[""])) == ["T1", "T2", "T3"]
    assert _ids(search_tenders_advanced(conn, None, org=[], category=["", "Works"])) \
        == ["T1"]
    assert _ids(search_tenders_advanced(conn, None, category=["  "])) \
        == ["T1", "T2", "T3"]


def test_filter_values_are_parameters_and_never_syntax(conn):
    """Values reach SQLite as bound parameters however many of them there are."""
    hostile = ["Works') OR 1=1 --", "'; DROP TABLE tenders; --"]
    assert _ids(search_tenders_advanced(conn, None, category=hostile)) == []
    assert _ids(search_tenders_advanced(conn, None, org=hostile)) == []
    assert conn.execute("SELECT count(*) FROM tenders").fetchone()[0] == 3


def test_org_prefix_is_a_prefix_and_not_a_like_pattern(conn):
    """Under LIKE the underscores in a real department name were wildcards."""
    conn.execute(
        "INSERT INTO tenders (tender_id, title, organisation_chain, source, status,"
        " first_seen_at, last_updated_at) VALUES"
        " ('T4','Clinic','Dean_VCRI_Theni','scraped','detailed','2025-05-04','2025-05-04'),"
        " ('T5','Clinic','DeanXVCRIXTheni','scraped','detailed','2025-05-05','2025-05-05')")
    assert _ids(search_tenders_advanced(conn, None, org=["Dean_VCRI_Theni"])) == ["T4"]
    # And '%' is a character somebody could paste, not "everything".
    assert _ids(search_tenders_advanced(conn, None, org=["%"])) == []


def test_multi_value_filters_still_use_their_indexes(conn):
    """A chip per department must not turn the filter into a table scan."""
    for where, params in (
        ("(t.organisation_chain >= ? AND t.organisation_chain < ?)"
         " OR (t.organisation_chain >= ? AND t.organisation_chain < ?)",
         ("Highways Department", "Highways Departmenu", "Rural", "Rurap")),
        ("t.tender_category IN (?,?)", ("Works", "Goods")),
        ("""json_extract(t.raw_json, '$."Form Of Contract"') IN (?,?)""",
         ("Works", "Supply")),
    ):
        plan = " ".join(
            r[3] for r in conn.execute(
                f"EXPLAIN QUERY PLAN SELECT t.tender_id FROM tenders t WHERE {where}",
                params))
        assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, (where, plan)


def test_prefix_range_bounds_are_equivalent_to_the_like_they_replaced(conn):
    from tenders.web.search import _prefix_range

    assert _prefix_range("MAWS") == ("MAWS", "MAWT")
    assert _prefix_range("PWD ") == ("PWD ", "PWD!")
    lo, hi = _prefix_range("Highways Department")
    rows = [r[0] for r in conn.execute(
        "SELECT tender_id FROM tenders WHERE organisation_chain >= ?"
        " AND organisation_chain < ? ORDER BY tender_id", (lo, hi))]
    like = [r[0] for r in conn.execute(
        "SELECT tender_id FROM tenders WHERE organisation_chain LIKE ?"
        " ORDER BY tender_id", ("Highways Department%",))]
    assert rows == like == ["T1", "T2"]


def test_criteria_filter(conn):
    rows, total = search_tenders_advanced(conn, None, criteria=("emd_exempt",))
    assert total == 1 and rows[0]["tender_id"] == "T1"


def test_criteria_indexes_cover_every_exposed_flag():
    """A partial index only applies when its predicate matches the WHERE term
    character for character, so a rename on either side silently unindexes the
    filter rather than breaking it."""
    assert set(CRITERIA.values()) == set(CRITERIA_FLAGS)


def test_advanced_filters_use_their_indexes(conn):
    """Guards against a filter quietly reverting to a full table scan."""
    for where, params in (
        ("t.tender_category = ?", ("Works",)),
        ("t.tender_value_num >= ?", (1.0,)),
        ("substr(t.published_date, 1, 10) >= ?", ("2025-01-01",)),
        ("""json_extract(t.raw_json, '$."Form Of Contract"') = ?""", ("Works",)),
    ):
        plan = " ".join(
            r[3] for r in conn.execute(
                f"EXPLAIN QUERY PLAN SELECT t.tender_id FROM tenders t WHERE {where}",
                params)
        )
        assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, (where, plan)


def test_cancelled_outranks_every_other_state():
    """A cancelled tender must never be advertised as still open."""
    from tenders.web.dashboard import tender_state
    future = "2099-01-01T15:00:00"
    assert tender_state(future)["label"] == "Live"
    assert tender_state(future, cancelled_at="2026-08-09T06:00:00+00:00")["label"] == "Cancelled"
    assert tender_state(None, cancelled_at="2026-08-09T06:00:00+00:00")["label"] == "Cancelled"


def test_dateless_tenders_are_not_labelled_unknown():
    """8,147 recovered records carry no dates; that is the portal's gap, not ours."""
    from tenders.web.dashboard import tender_state
    state = tender_state(None, None)
    assert state["label"] == "No dates"
    assert state["hint"]


def test_corrigenda_parsing():
    from tenders.web.app import _corrigenda
    assert _corrigenda({"corrigenda_json": None}) == []
    assert _corrigenda({"corrigenda_json": "not json"}) == []
    assert _corrigenda({"corrigenda_json": '{"a": 1}'}) == []
    assert _corrigenda({"corrigenda_json":
                        '[{"title":"Date Corrigendum","type":"Date"},{"junk":1}]'}) == [
        {"title": "Date Corrigendum", "type": "Date"}]


def test_placeholder_sizes_are_not_reported_as_sizes():
    from tenders.web.app import _format_filesize
    assert _format_filesize(None, "NA") == ""
    assert _format_filesize(None, "512") == "512 KB"
    assert _format_filesize(2 * 1024 * 1024) == "2.0 MB"


def test_award_state_precedence():
    """Awarded outranks the dates; cancelled outranks the award.

    The page draws one seal from an if/elif on the same two facts, so a
    disagreement here is a page that stamps CANCELLED and AWARDED at once.
    """
    from tenders.web.dashboard import tender_state
    future, aoc = "2099-01-01T15:00:00", "2025-09-18T15:51:00"
    assert tender_state(future, awarded_at=aoc)["label"] == "Awarded"
    assert tender_state(None, awarded_at=aoc)["label"] == "Awarded"
    assert tender_state(future, cancelled_at="2026-01-01T00:00:00",
                        awarded_at=aoc)["label"] == "Cancelled"


def test_award_panel_never_renders_for_a_cancelled_tender():
    from tenders.web.dashboard import award_panel
    awarded = {"awarded_at": "2025-09-18T15:51:00", "award_stage": "AOC"}
    assert award_panel(awarded) is not None
    assert award_panel(awarded | {"cancelled_at": "2026-01-01T00:00:00"}) is None
    assert award_panel({"tender_id": "x"}) is None


def test_award_panel_compares_estimate_against_accepted_bid():
    """The gap between the two is the finding; it must be computed, not implied."""
    from tenders.web.dashboard import award_panel
    p = award_panel({"awarded_at": "2025-09-18T15:51:00",
                     "tender_value_num": 11900000.0, "award_value_num": 11629379.22})
    assert p["estimate"] == "₹1.19 Cr" and p["value"] == "₹1.16 Cr"
    assert p["delta"]["direction"] == "below"
    assert round(p["delta"]["pct"], 1) == 2.3
    over = award_panel({"awarded_at": "x", "tender_value_num": 500000.0,
                        "award_value_num": 900000.0})
    assert over["delta"]["direction"] == "above" and over["delta"]["css"] == "aw-over"
    # No estimate to compare against must not fabricate one.
    assert award_panel({"awarded_at": "x", "award_value_num": 5.0})["delta"] is None


def test_inr_exact_uses_indian_grouping():
    from tenders.web.dashboard import _inr_exact
    assert _inr_exact(11629379.22) == "1,16,29,379.22"
    assert _inr_exact(395305.5) == "3,95,305.50"
    assert _inr_exact(999) == "999.00"


def test_share_card_never_prints_a_missing_value():
    """Owner's rule: no "Value NA" chip, ever — the type chip stands in, once."""
    from tenders.web.sharecard import facts
    chips = [c["text"] for c in facts({"tender_type": "Open Tender",
                                       "tender_value_raw": "NA"})]
    assert chips.count("Open Tender") == 1
    assert not any("NA" in c for c in chips)


def test_share_card_separates_estimate_from_award():
    from tenders.web.sharecard import facts
    chips = facts({"tender_type": "Open Tender", "tender_value_num": 11900000.0,
                   "awarded_at": "2025-09-18T15:51:00", "award_value_num": 11629379.22,
                   "awarded_to": "RVS CONSTRUCTIONS"})
    by_text = {c["text"]: c["tone"] for c in chips}
    assert by_text["Est. ₹1.19 Cr"] == "money"
    assert by_text["Won ₹1.16 Cr"] == "award"
    assert by_text["RVS CONSTRUCTIONS"] == "award"


def test_award_section_label_matches_the_enricher():
    """award_panel filters documents by a literal; a drift would hide the AOC file."""
    from tenders.enrich_awards import AOC_SECTION
    from tenders.web.dashboard import award_panel
    p = award_panel({"awarded_at": "x"},
                    [{"section": AOC_SECTION, "id": 1}, {"section": "NIT Document"}])
    assert [d["id"] for d in p["docs"]] == [1]


def test_relative_close_uses_ist_calendar_days_not_a_24_hour_span():
    """"Today" is a question about the calendar in Chennai, not about elapsed hours.

    A deadline at 17:30 tonight and one at 10:00 tomorrow are 16 hours apart,
    and comparing raw timedeltas puts both in the same bucket.
    """
    from datetime import datetime, timezone
    from tenders.web.dates import relative_close, relative_short

    # 2026-08-09 12:00 UTC == 17:30 IST.
    now = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
    assert relative_close("2026-08-09T23:30:00", now) == "Closes today"
    assert relative_close("2026-08-10T10:00:00", now) == "Closes tomorrow"
    assert relative_close("2026-08-14T17:30:00", now) == "Closes in 5 days"
    assert relative_close("2026-11-09T17:30:00", now) == "Closes in 3 months"
    assert relative_close("2025-08-09T17:30:00", now) == "Closed 1 year ago"
    # Past instants on today's date must never read as still open.
    assert relative_close("2026-08-09T09:00:00", now) == "Closed today"
    assert relative_close(None, now) == ""
    assert relative_close("not a date", now) == ""
    assert relative_short("2026-08-14T17:30:00", now) == "in 5 days"


def test_display_dates_never_reformat_the_stored_value():
    from tenders.web.dates import fmt_date, fmt_datetime

    assert fmt_date("2026-08-13T15:00:00") == "13-August-2026"
    assert fmt_datetime("2026-08-13T15:00:00") == "13-August-2026 · 15:00"
    # A bare midnight is what the parser leaves on a date-only string; printing
    # it would state a deadline hour the portal never published.
    assert fmt_datetime("2026-08-13T00:00:00") == "13-August-2026"
    assert fmt_date(None) == ""


def test_headline_never_trims_a_title_down_to_one_word():
    """The bug this guards: the old trim cut at the first preposition, so
    "Drilling of New Borewell at Kannimar Nagar" rendered as "Drilling"."""
    from tenders.shortnames import headline, is_uninformative

    for title in ("Drilling of New Borewell at Kannimar Nagar in Ward No 20, North Zone",
                  "Hiring of vehicle for the official use of Assistant Executive Engineer",
                  "Application of rubber lining in the inner surface of storage Tank",
                  "Purchase of 27,000 Kgs. of LDPE Liner for Packing 25 Kg. SMP"):
        assert not is_uninformative(headline({"title": title})), title


def test_headline_composes_from_other_fields_when_the_title_names_nothing():
    """~5% of titles are a filing reference. No summariser can expand those."""
    from tenders.shortnames import headline

    assert headline({"title": "OT 44/25-26",
                     "work_description": "Overhauling of Auxiliary Cooling Water pump"}) \
        == "Overhauling of Auxiliary Cooling Water pump"
    # No usable description either ("REFER PDF" names nothing) — the portal's own
    # classification still says more than the reference does.
    assert headline({"title": "OT-23", "work_description": "REFER PDF",
                     "product_category": "Machinery and Machining Tools",
                     "location": "Ariyalur"}) == "Machinery and Machining Tools · Ariyalur"
    # Nothing anywhere: the reference is the honest answer, not a blank.
    assert headline({"title": "Z.O.1.C.No.B2/01996/2025"})


def test_machine_keys_are_spelled_out_but_abbreviations_survive():
    """Shouting is normalised; abbreviations are not.

    An earlier version of this rule re-cased only values containing an
    underscore, on the theory that "TANGEDCO" would otherwise become
    "Tangedco". Measured over the whole archive that fear turned out to be
    unfounded — the two guards below already cover it — while 1,025 distinct
    values across ~18,000 tender references were left bellowing at the reader.
    So the licence is now "the value contains no lowercase letter", and the
    burden moves entirely onto the abbreviation guards, which is what these
    assertions pin down.
    """
    from tenders.shortnames import pretty_name

    # The reported value, and the shape of it.
    assert pretty_name("GREATER_CHENNAI_CORPORATION_ZONE_14") == \
        "Greater Chennai Corporation Zone 14"
    assert pretty_name("TNEB_LIMITED") == "TNEB Limited"
    assert pretty_name("TANGEDCO_ZONE_3") == "TANGEDCO Zone 3"
    # No vowel, so an abbreviation by construction — no list needed.
    assert pretty_name("PWD_BUILDINGS_DIVISION") == "PWD Buildings Division"
    assert pretty_name("CMWSS_BOARD_AREA_2") == "CMWSS Board Area 2"
    # Anything already carrying a lowercase letter was cased by a person.
    assert pretty_name("Dean_VCRI_Theni") == "Dean VCRI Theni"
    assert pretty_name("Block Development Office_Thally") == \
        "Block Development Office Thally"
    # Shouted names are now spelled out — this is the reported complaint.
    assert pretty_name("CHENNAI RIVERS TRANSFORMATION COMPANY LIMITED") == \
        "Chennai Rivers Transformation Company Limited"
    assert pretty_name("HOSUR CITY MUNICIPAL CORPORATION") == \
        "Hosur City Municipal Corporation"
    # ...but the coded place strings keep their suffixes: RD (Rural Development)
    # and TN carry no vowel, so the structural guard holds them without a list.
    assert pretty_name("ANAICUT, VELLORE,RD,TN") == "Anaicut, Vellore,RD,TN"

    # Still verbatim. Named abbreviations, vowel-less ones, two-letter offices,
    # and anything a person already cased.
    for verbatim in ("TNEB Limited", "TANGEDCO", "MAWS", "PWD", "TTPS",
                     "CE-MTPS-II - TANGEDCO", "SE C and M", "CoC"):
        assert pretty_name(verbatim) == verbatim
    assert pretty_name(None) == "" and pretty_name("") == ""


def test_headline_drops_a_dangling_count_rather_than_printing_it():
    from tenders.shortnames import headline

    assert headline({"title": "Construction of Community Soak Pit at Vanagaram "
                              "Panchayat 5 Nos"}).rstrip("…").endswith("Panchayat")


# ---------------------------------------------------------------------------
# Document excerpts. These guard the thing that makes the archive worth using:
# a search finds a phrase buried on page 40 of a scan, and the result has to
# show that phrase. An excerpt that comes back empty, or that always shows the
# opening of the file, makes a real deep match read as a false positive.

DEEP = "antibuckling"
FILLER = ("The Contractor shall provide all materials and labour. " * 4000)


@pytest.fixture()
def docs(tmp_path):
    """A corpus with the shapes that break excerpting: a multi-megabyte scan
    with the term only near its end, a phrase whose words also appear apart,
    OCR containing HTML, accented text the index folds, and a filename-only
    match."""
    import sqlite3 as _sq

    db = tmp_path / "d.db"
    init_db(db)
    c = _sq.connect(db)
    c.row_factory = _sq.Row
    c.execute(
        "INSERT INTO tenders (tender_id, title, organisation_chain, closing_date,"
        " published_date, source, status, first_seen_at, last_updated_at)"
        " VALUES ('T1','Works','Highways Department','2025-06-01T15:00:00',"
        " '2025-05-01T10:00:00','scraped','detailed','2025-05-01','2025-05-01')"
    )
    texts = {
        # ~1.5 MB, and the only occurrence sits about 85% of the way in.
        "deep.pdf": FILLER + f" Clause 47.3 {DEEP} restraint devices shall be "
                             "fitted to every span. " + FILLER[:200000],
        # "storm" and "water" each occur early and apart; the phrase is later.
        "phrase.pdf": "storm damage report. water tanker schedule. " + ("x " * 400)
                      + "Construction of storm water drain at Mandaveli Street.",
        "xss.pdf": "Invoice for drain works <script>alert('xss')</script> "
                   "& <img src=x onerror=alert(1)> filed by A & B <b>Ltd</b>.",
        # Legacy font-encoded Tamil: the index folds ä to a, a literal scan does not.
        "tamil.pdf": "ehŸ 24.07.2026 ä‹dQ x¥gªjòŸë drainage works",
        "byname.pdf": "This file mentions nothing of interest at all.",
        # 35,000 copies of the term, so the scan must not collect them all.
        "many.pdf": ("drain " * 35000),
    }
    for i, (name, text) in enumerate(texts.items(), start=1):
        c.execute(
            "INSERT INTO documents (id, tender_id, filename, section, status)"
            " VALUES (?,'T1',?,'NIT','captured')", (i, name))
        c.execute(
            "INSERT INTO doc_text (document_id, text, method, char_count)"
            " VALUES (?,?,'pdf_layer',?)", (i, text, len(text)))
    # Built through the production path, so the test index is the same shape
    # the site searches: external content over docs_fts_source, rowid == id.
    ensure_docs_fts_aligned(c)
    c.commit()
    yield c
    c.close()


def _docs_for(conn, query, limit=25):
    from tenders.web.search import parse_terms

    return search_documents(conn, build_match(query), limit, 0, parse_terms(query))[0]


def _by_name(rows):
    return {r["filename"]: r for r in rows}


def test_a_term_buried_deep_in_a_multi_megabyte_document_is_excerpted(docs):
    """The whole point of the archive. The term is ~1.3 MB into the file, which
    is exactly the case snippet() was too slow for and a head-of-file excerpt
    would misreport as a false positive."""
    row = _by_name(_docs_for(docs, DEEP))["deep.pdf"]
    assert row["snip_kind"] == "text"
    assert f"<mark>{DEEP}</mark>" in row["snip"]
    # Not the opening of the file, and carrying the context that identifies it.
    assert "Clause 47.3" in row["snip"] and "restraint devices" in row["snip"]


def test_a_phrase_is_excerpted_at_the_phrase_not_at_a_loose_word(docs):
    """`"storm water"` matched because the words are adjacent; showing the
    reader the unrelated "storm damage" at the top of the file would claim a
    match that FTS5 never made."""
    row = _by_name(_docs_for(docs, '"storm water"'))["phrase.pdf"]
    assert "<mark>storm water</mark>" in row["snip"]
    assert "storm damage" not in row["snip"]


def test_a_prefix_term_marks_a_longer_word_but_never_a_suffix():
    """`drain` is compiled to `"drain"*`, so it matched drainage and did not
    match underdrain. Highlighting the second would be a lie about the match."""
    from tenders.web.excerpt import Term, compile_terms, excerpt

    pats = compile_terms([Term(("drain",), True)])
    assert "<mark>drain</mark>age" in excerpt("Storm drainage layout", pats)
    assert excerpt("The underdrain was replaced", pats) is None


def test_ocr_text_is_escaped_before_it_is_marked_safe(docs):
    """The excerpt is rendered with |safe and its content is OCR of an arbitrary
    uploaded PDF. snippet() escaped nothing but produced no markup either; this
    builds markup, so the text around it has to be escaped here."""
    snip = _by_name(_docs_for(docs, "drain"))["xss.pdf"]["snip"]
    assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in snip
    assert "&lt;img src=x onerror=alert(1)&gt;" in snip
    assert "A &amp; B" in snip
    # The decisive property: the only '<' in the output are the ones we wrote.
    # An attacker who cannot open a tag cannot do anything with the rest.
    assert snip.count("<") == snip.count("<mark>") + snip.count("</mark>")
    assert "<mark>drain</mark>" in snip


def test_accented_text_is_found_the_way_the_index_folds_it(docs):
    """The index is built `remove_diacritics 2`, so a large part of this corpus —
    legacy font-encoded Tamil that extracts as accented Latin — is indexed under
    unaccented tokens. A literal scan finds none of it."""
    rows = _by_name(_docs_for(docs, "a"))
    assert rows["tamil.pdf"]["snip_kind"] == "text"
    assert "<mark>ä</mark>" in rows["tamil.pdf"]["snip"]


def test_a_result_that_matched_only_its_filename_says_so(docs):
    """docs_fts indexes the filename too, so a hit need not be in the text at
    all. Silently showing the first paragraph is what made deep matches look
    false; the row has to declare which kind of excerpt it is."""
    rows = _by_name(_docs_for(docs, "byname"))
    assert rows["byname.pdf"]["snip_kind"] == "filename"


def test_a_word_repeated_tens_of_thousands_of_times_is_not_all_collected(docs):
    """A 3 MB OCR blob can hold 35,000 copies of a common word and the excerpt
    shows one of them. Collecting them all measured 96 ms against 0.3 ms."""
    import re

    from tenders.web.excerpt import (
        _MAX_HITS, _WINDOW, Term, compile_terms, _scan)

    pats = compile_terms([Term(("drain",), True)])
    assert len(_scan("drain " * 35000, pats)) <= _MAX_HITS
    # Every occurrence inside the window is still marked — that is honest — but
    # the window itself stays one excerpt rather than growing with the file.
    snip = _by_name(_docs_for(docs, "drain"))["many.pdf"]["snip"]
    assert len(re.sub(r"</?mark>", "", snip)) < 3 * _WINDOW


def test_the_python_excerpt_did_not_change_which_documents_match(docs):
    """Matching, ranking and paging are untouched: only the text of snip moves
    off FTS5. Compared against the snippet() query this replaced."""
    from tenders.web.search import parse_terms

    old = """
        WITH hits AS MATERIALIZED (
            SELECT document_id,
                   snippet(docs_fts, 3, '<mark>', '</mark>', ' … ', 18) AS snip,
                   rank AS rank
            FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?
        )
        SELECT d.id AS document_id, h.rank AS rank
        FROM hits h JOIN documents d ON d.id = h.document_id ORDER BY h.rank
    """
    for query in ("drain", "storm water drain", '"storm water"', DEEP, "works",
                  "nothing zzqq", "a", "drainage", "underdrain", "road AND drain"):
        match = build_match(query)
        want = [(r[0], r[1]) for r in docs.execute(old, (match, 25, 0)).fetchall()]
        want_total = docs.execute(
            "SELECT count(*) FROM docs_fts WHERE docs_fts MATCH ?", (match,)
        ).fetchone()[0]
        got, total = search_documents(docs, match, 25, 0, parse_terms(query))
        assert [(r["document_id"], r["rank"]) for r in got] == want, query
        assert total == want_total, query


def test_the_fts_expression_is_byte_identical_after_the_parse_was_shared():
    """build_match and parse_terms were split out of one function so the
    excerpt could see what the match saw. The match itself must not have moved:
    every stored watch and every shared link depends on it."""
    for query in ["road", "road AND bridge", '"storm water" drain', "road-work",
                  "a", "(pump)", '"', "* *", "AND OR NOT NEAR", "", "  ",
                  "e-procurement", "storm.water", "2025"]:
        rebuilt = build_match(query)
        assert rebuilt == _reference_build_match(query)


def _reference_build_match(query):
    """The pre-split implementation, kept verbatim as the thing to agree with."""
    import re

    phrase_re = re.compile(r'"([^"]+)"')
    special = re.compile(r'[\^\*\(\)\":\-+]')
    if not query or not query.strip():
        return None
    parts = []
    for m in phrase_re.finditer(query):
        phrase = m.group(1).strip().replace('"', "")
        if phrase:
            parts.append(f'"{phrase}"')
    for token in phrase_re.sub(" ", query).split():
        clean = special.sub("", token).strip()
        if len(clean) >= 2:
            parts.append(f'"{clean}"*')
        elif clean:
            parts.append(f'"{clean}"')
    return " ".join(parts) if parts else None


def test_a_character_is_marked_if_and_only_if_the_tokenizer_matched_it():
    """The excerpt claims a passage is why a document came back, so its idea of
    "same character" has to be the index's idea, exactly.

    Both directions were wrong at different points and both were caught here.
    Deriving folds from Unicode NFD over-folded 228 codepoints SQLite leaves
    alone (all of Greek Extended), which would have marked passages that never
    matched. Relying on re.IGNORECASE over-matched 350 pairs SQLite does not
    case-fold, Turkish dotless i against i among them. Asking the tokenizer for
    the character classes and using them *instead of* IGNORECASE agrees on every
    pair; over the full BMP range that is 41.1 M pairs, and the slice below is
    the dense part of it plus the specific characters that broke it.
    """
    import sqlite3 as _sq

    from tenders.web.excerpt import Term, compile_term, _load_folds

    _load_folds()
    c = _sq.connect(":memory:")
    c.execute("CREATE VIRTUAL TABLE f USING fts5(t, "
              "tokenize='unicode61 remove_diacritics 2')")
    c.execute("CREATE VIRTUAL TABLE v USING fts5vocab(f, 'instance')")
    # Latin through Latin Extended-A, plus the traps: dotless i, dotted I, micro
    # sign, long s, Kelvin sign, and a Greek Extended letter SQLite leaves alone.
    points = [cp for cp in range(0x1, 0x180) if chr(cp).isalnum()]
    points += [0x131, 0x130, 0xB5, 0x3BC, 0x17F, 0x212A, 0x1F00, 0x391, 0x3B1]
    rows = [(cp, chr(cp)) for cp in sorted(set(points))]
    c.executemany("INSERT INTO f(rowid, t) VALUES (?, ?)", rows)
    token = {chr(doc): term for term, doc in c.execute("SELECT term, doc FROM v")}

    for _, query_char in rows:
        pattern = compile_term(Term((query_char,), False)).exact
        for _, text_char in rows:
            indexed_alike = token.get(text_char) == token.get(query_char)
            assert bool(pattern.fullmatch(text_char)) is indexed_alike, (
                query_char, text_char, token.get(query_char), token.get(text_char))
