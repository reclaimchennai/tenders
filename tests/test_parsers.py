"""Regression tests for the HTML parsers and captcha OCR, using real fixtures
captured from tntenders.gov.in.
"""

from pathlib import Path

import pytest

from tenders.parse_detail import parse_detail
from tenders.parse_listing import parse_org_tree, parse_tender_list

FIX = Path(__file__).parent / "fixtures"
HOST = "https://tntenders.gov.in"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8", errors="replace")


def test_detail_expired_docs_are_lost():
    r = parse_detail(_read("detail_expired.html"))
    assert r["fields"]["Tender ID"] == "2025_CMDAC_587614_1"
    assert r["fields"]["EMD Amount in ₹"] == "93,500"
    assert len(r["documents"]) == 4
    # Expired tender: every document is gone (no download link).
    assert all(d["download_url"] is None for d in r["documents"])
    names = {d["filename"] for d in r["documents"]}
    assert "Tendernotice_1.pdf" in names
    assert "BOQ_702047.xls" in names


def test_detail_active_has_live_doc():
    r = parse_detail(_read("detail_active.html"))
    assert r["fields"]["Tender ID"] == "2026_ARCL_674458_1"
    live = [d for d in r["documents"] if d["download_url"]]
    assert any(d["filename"] == "Tendernotice_1.pdf" for d in live)
    # The live download link points at the docDownoad servlet.
    assert any("docDownoad" in (d["download_url"] or "") for d in live)


def test_org_tree_parsing():
    orgs = parse_org_tree(_read("org_tree.html"), HOST)
    assert len(orgs) > 20
    names = {o["name"] for o in orgs}
    assert any("Arasu Rubber" in n for n in names)
    assert all(o["drill_url"].startswith(HOST) for o in orgs)


def test_tender_list_parsing_and_stable_permalinks():
    tenders = parse_tender_list(_read("org_tenderlist.html"), HOST)
    assert len(tenders) >= 20
    for t in tenders:
        assert t["tender_id"].count("_") >= 3  # e.g. 2026_ARCL_674458_1
        # Stable permalink: session token stripped.
        assert "session=T" not in t["detail_url"]
        assert "FrontEndViewTender" in t["detail_url"]


def test_org_listing_carries_dates_and_a_split_title():
    """Discovery must not throw away what the listing already tells us.

    The row's single "[Title] [Ref][Tender ID]" cell used to be stored verbatim
    as the title (brackets and all, reference number lost), and the three
    timestamps beside it were dropped entirely — leaving every not-yet-detailed
    tender undateable, and so invisible to short-bid-window detection.
    """
    t = next(x for x in parse_tender_list(_read("org_tenderlist.html"), HOST)
             if x["tender_id"] == "2026_ARCL_674458_1")
    assert not t["title"].startswith("[")
    assert "]" not in t["title"]
    assert t["reference_number"] == "D1/6506/24-6"
    assert t["published_raw"] == "14-Mar-2026 09:00 AM"
    assert t["closing_raw"] == "24-Jun-2026 05:30 PM"
    assert t["opening_raw"] == "27-Jun-2026 11:30 AM"


def test_latest_active_rows_are_newest_first_with_dates_and_permalinks():
    from tenders.latest_active import parse_latest_rows
    from tenders.util import parse_date

    rows = parse_latest_rows(_read("latest_active.html"), HOST)
    assert len(rows) == 10
    first = rows[0]
    assert first["tender_id"] == "2026_DTP_692629_1"
    assert first["title"] == "SDRF 2026-2027(Manalurpet TP)"
    assert first["reference_number"] == "313/2026/A1"
    assert first["value_raw"] == "32,00,000"
    # Sorted newest-published-first: this ordering is what makes a shallow poll
    # a complete one, so a regression here silently breaks the detector.
    published = [parse_date(r["published_raw"]) for r in rows]
    assert published == sorted(published, reverse=True)
    # The row link names its own page; it must be rewritten to the permalink
    # form the organisation-tree walker stores, or discovery here is unusable
    # once the session ends.
    for r in rows:
        assert "FrontEndViewTender" in r["detail_url"]
        assert "session=T" not in r["detail_url"]
        assert "FrontEndLatestActive" not in r["detail_url"]


def test_corrigendum_listing_shares_the_row_shape():
    # Same table, one different trailing column plus a stray empty cell.
    from tenders.latest_active import parse_latest_rows

    rows = parse_latest_rows(_read("latest_corrigendums.html"), HOST)
    assert len(rows) == 10
    assert rows[0]["tender_id"] == "2026_DTP_692420_1"
    assert rows[0]["title"] == "Mini Power Pump in Melamanjamedu"
    assert rows[0]["closing_raw"] == "17-Aug-2026 03:00 PM"


def test_short_window_tender_is_flagged_from_a_listing_row_alone(tmp_path):
    """A listing row must be enough to raise the flag.

    The tender we systematically miss opens and closes the same afternoon, so
    the flag cannot wait for a detail fetch that may never happen before the
    portal deletes the tender.
    """
    from tenders.db import connect, init_db
    from tenders.latest_active import record_row

    db = tmp_path / "t.db"
    init_db(db)
    conn = connect(db)
    try:
        outcome, stored = record_row(conn, {
            "tender_id": "2026_RDTN_684543_2",
            "title": "Short window work", "reference_number": "R/1",
            "organisation_chain": "Rural Development",
            "published_raw": "05-Aug-2026 05:00 PM",
            "closing_raw": "05-Aug-2026 05:24 PM",
            "opening_raw": "05-Aug-2026 05:30 PM",
            "value_raw": "12,00,000",
            "detail_url": HOST + "/nicgep/app?component=%24DirectLink&page="
                                 "FrontEndViewTender&service=direct&sp=Sabc%3D%3D",
        })
        assert outcome == "new"
        assert stored["published_date"] == "2026-08-05T17:00:00"
        flag = conn.execute("SELECT * FROM redflags WHERE tender_id=?",
                            ("2026_RDTN_684543_2",)).fetchone()
        assert flag["reason"] == "short_bid_window"
        assert flag["severity"] == "high"
        assert flag["window_hours"] == 0.4
        # sp_token is stored alongside the permalink so the two discovery paths
        # produce interchangeable rows.
        assert conn.execute(
            "SELECT sp_token FROM tenders WHERE tender_id=?",
            ("2026_RDTN_684543_2",)).fetchone()[0] == "Sabc=="
    finally:
        conn.close()


def test_recording_a_listing_row_never_downgrades_a_detailed_tender(tmp_path):
    # The poll re-sees tenders every few minutes; it must enrich them, never
    # overwrite the richer detail-page record with the thinner listing one.
    from tenders.db import connect, init_db
    from tenders.latest_active import record_row

    db = tmp_path / "t.db"
    init_db(db)
    conn = connect(db)
    try:
        conn.execute(
            "INSERT INTO tenders (tender_id, title, status, source,"
            " first_seen_at, last_updated_at, published_date, closing_date)"
            " VALUES ('2026_X_1_1', 'Real title', 'detailed', 'scraped',"
            " '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00',"
            " '2026-08-01T09:00:00', '2026-08-10T15:00:00')")
        record_row(conn, {
            "tender_id": "2026_X_1_1", "title": "Listing title",
            "reference_number": "R/2", "organisation_chain": "Org",
            "published_raw": "01-Aug-2026 09:00 AM",
            # A corrigendum moved the deadline; the live listing wins on that.
            "closing_raw": "20-Aug-2026 03:00 PM",
            "opening_raw": None, "value_raw": None, "detail_url": None,
        })
        row = conn.execute("SELECT * FROM tenders WHERE tender_id='2026_X_1_1'").fetchone()
        assert row["status"] == "detailed"
        assert row["title"] == "Real title"
        assert row["closing_date"] == "2026-08-20T15:00:00"
        assert row["reference_number"] == "R/2"  # filled, because it was missing
    finally:
        conn.close()


def test_status_list_parsing():
    from tenders.enumerate_status import parse_status_rows

    rows = parse_status_rows(_read("status_list_cancelled.html"))
    assert len(rows) == 10
    first = rows[0]
    assert first["tender_id"] == "2025_WRD_596633_1"
    assert first["stage"] == "Cancelled"
    assert first["organisation_chain"].startswith("Water Resources Department||")
    # The single "[Title][Ref.No.]" cell must be split, not stored verbatim.
    assert "[" not in first["title"]
    assert first["reference_number"] == (
        "e-Tender Notice No.1 /2025-2026 /LCBC-TNJ./dated.19.08.2025")


def test_status_month_windows_are_calendar_aligned():
    from tenders.enumerate_status import month_windows

    w = month_windows("2025-11", "2026-02")
    assert w[0] == ("01/11/2025", "30/11/2025")
    assert w[-1] == ("01/02/2026", "28/02/2026")
    assert len(w) == 4


def test_links_from_post_captcha_success_page():
    # The post-captcha page is structurally a detail page; parse_detail is the
    # single source of truth for its streaming links.
    r = parse_detail(_read("docdown_success.html"))
    links = {d["filename"]: d["download_url"]
             for d in r["documents"] if d["download_url"]}
    assert "Tendernotice_1.pdf" in links
    assert "DirectLink_0" in links["Tendernotice_1.pdf"]


def test_download_as_zip_bundle_is_captured():
    # The 'Download as zip file' bulk link must be captured — it is the only
    # source for work-item documents (e.g. BOQ spreadsheets).
    r = parse_detail(_read("docdown_success.html"))
    zips = [d for d in r["documents"] if d.get("document_type") == "zip-bundle"]
    assert zips, "expected a zip-bundle document"
    assert all(d["download_url"] for d in zips)
    assert all(d["filename"].endswith(".zip") for d in zips)


def test_captcha_ocr_on_sample():
    from PIL import Image
    from tenders.captcha import preprocess, ocr, tesseract_available

    if not tesseract_available():
        pytest.skip("tesseract not installed")
    img = Image.open(FIX / "captcha_e6p78P.png")
    assert ocr(preprocess(img)) == "e6p78P"


def test_captcha_cnn_on_sample():
    """The trained CNN must read the fixture Tesseract gets wrong.

    This fixture is not in data/captcha/raw, so it is genuinely held out from
    training, and its label exercises case (e/p lowercase, P uppercase) — the
    axis the model is weakest on.
    """
    from PIL import Image
    from tenders.captcha_model import TrainedSolver
    from tenders.config import load_config

    solver = TrainedSolver.get(load_config())
    if solver is None:
        pytest.skip("no trained captcha model above the usable threshold")
    assert solver.predict(Image.open(FIX / "captcha_e6p78P.png")) == "e6p78P"


def test_captcha_synth_matches_portal_geometry():
    """The synthetic generator must keep producing portal-shaped captchas.

    Training depends entirely on the reconstruction staying faithful, and the
    failure mode is silent — a font substitution or a stray anti-aliasing
    change would still yield plausible-looking images while quietly destroying
    accuracy. Pin the properties that were measured off the real captchas.
    """
    import random

    from tenders import captcha_synth

    if not captcha_synth.available():
        pytest.skip("DejaVu Sans not installed")
    rng = random.Random(0)
    for _ in range(20):
        img, text = captcha_synth.make(rng)
        assert len(text) == 6
        assert img.height == 45
        assert 170 <= img.width <= 235
        colours = {c for _, c in img.convert("RGB").getcolors(maxcolors=1 << 20)}
        assert (0, 0, 0) in colours and (255, 255, 255) in colours


def test_latest_watch_paces_itself_and_survives_a_failing_poll(monkeypatch):
    """The watch is ticked from inside long passes, so it has to be inert when
    not due, must never propagate a failure into the pass that ticked it, and
    must stop itself if it starts burning requests (nothing else bounds it —
    scrape.max_requests_per_run is 0 in this deployment)."""
    from tenders import latest_active
    from tenders.config import load_config

    calls = []

    class FakeClient:
        request_count = 0

    watch = latest_active.LatestWatch(load_config(), interval_s=10_000)

    def fake_poll(conn, client, cfg, **kw):
        calls.append(1)
        return {"ok": True, "new": 1, "docs_captured": 0}

    monkeypatch.setattr(latest_active, "poll_latest", fake_poll)

    assert watch.tick(None, FakeClient()) is not None      # due at construction
    assert len(calls) == 1
    assert watch.tick(None, FakeClient()) is None          # interval not elapsed
    assert watch.tick(None, FakeClient(), force=True) is not None
    assert len(calls) == 2

    def boom(conn, client, cfg, **kw):
        raise RuntimeError("portal said no")

    monkeypatch.setattr(latest_active, "poll_latest", boom)
    assert watch.tick(None, FakeClient(), force=True) is None
    assert watch.totals["failed"] == 1

    monkeypatch.setattr(latest_active, "poll_latest", fake_poll)
    watch._hour_requests = int(watch.opts["max_requests_per_hour"])
    assert watch.tick(None, FakeClient(), force=True) is None
    assert watch.totals["throttled"] == 1
    assert len(calls) == 2


def test_status_rows_carry_a_usable_permalink():
    """The Tenders-Status listing's row link is a permalink in disguise.

    Its ``sp`` token is a tender key, not a session key, so rewriting the page
    parameter turns a "register entry only" row into an addressable detail
    page — the difference between knowing a gap-period tender existed and
    being able to recover its dates.
    """
    from tenders.enumerate_status import parse_status_rows

    rows = parse_status_rows(_read("status_list_cancelled.html"), HOST)
    assert rows[0]["tender_id"] == "2025_WRD_596633_1"
    for r in rows:
        assert "FrontEndViewTender" in r["detail_url"]
        assert "session=T" not in r["detail_url"]
        assert "WebTenderStatusLists" not in r["detail_url"]


def test_latest_search_enters_through_clear_so_the_form_is_never_stale(monkeypatch):
    """The poll must not build its POST from a results-bearing render.

    Tapestry keeps the result table in the page's session state, so every
    render after the first successful search carries the rows *and* a formids
    list grown from 10 component ids to 25. Echo that back and the server
    answers "Stale Link" before it looks at the captcha — which is
    indistinguishable, in the log, from a rejected captcha. That is exactly how
    this poll spent a day failing 6/6 on every attempt with a solver that was
    working perfectly. Entering through the page's own Clear link is the fix,
    so the entry URL and the submitted formids are both pinned here.
    """
    from tenders import latest_active
    from tenders.config import load_config

    posted = {}

    class FakeClient:
        request_count = 0
        gets: list = []

        def get(self, url, **kw):
            self.gets.append(url)
            # Whatever the poll asks for, the portal is holding a previous
            # search's results — unless the poll asks for them to be cleared.
            name = ("latest_active_form.html" if "component=clear" in url
                    else "latest_active.html")
            return type("R", (), {"text": _read(name), "status_code": 200})()

        def post(self, url, data=None, **kw):
            posted.update(data)
            return type("R", (), {"text": _read("latest_active.html"),
                                  "status_code": 200})()

    monkeypatch.setattr(latest_active, "solve_image", lambda src, **kw: "abc123")
    monkeypatch.setattr(latest_active, "save_verified_label",
                        lambda *a, **kw: None)
    client = FakeClient()
    assert latest_active._search(client, load_config(), latest_active.LATEST_TENDERS,
                                 sort=latest_active._SORT_PUBLISHED, attempts=1)
    assert "component=clear" in client.gets[0]
    assert "iterRows" not in posted["formids"]
    assert posted["size"] == latest_active._SORT_PUBLISHED
    assert posted["captchaText"] == "abc123"


def test_a_stale_link_rejection_is_not_reported_as_a_bad_captcha(monkeypatch, caplog):
    """The two failures are indistinguishable to a "did rows come back?" test,
    and only one of them is worth another captcha. Keeping them apart in the
    log is what stops the next reader hunting the solver instead of the form,
    which is how the original bug survived a day of 6/6 "captcha rejected"."""
    import logging

    from tenders import latest_active
    from tenders.config import load_config

    stale = ('<html><head><title>Stale Link</title></head><body>'
             '<span class="exception-message" id="Insert">Rewind of form '
             "expected 9 more form elements, starting with id 'If_33'."
             "</span></body></html>")

    class FakeClient:
        request_count = 0

        def get(self, url, **kw):
            return type("R", (), {"text": _read("latest_active_form.html"),
                                  "status_code": 200})()

        def post(self, url, data=None, **kw):
            return type("R", (), {"text": stale, "status_code": 200})()

    monkeypatch.setattr(latest_active, "solve_image", lambda src, **kw: "abc123")
    notes = []
    with caplog.at_level(logging.WARNING, logger="latest"):
        assert latest_active._search(FakeClient(), load_config(),
                                     latest_active.LATEST_TENDERS, attempts=1,
                                     progress=notes.append) is None
    assert "If_33" in caplog.text and "stale link" in caplog.text
    assert any("stale-link" in n for n in notes)
    assert not any("captcha rejected" in n for n in notes)


def test_only_a_call_site_that_confirms_can_trip_the_captcha_breaker(monkeypatch):
    """The unconfirmed-solve tripwire is process-global, so a page that solves
    captchas and never says whether they were accepted must not be able to move
    it. One did: the newest-first poll's broken form drove the counter to its
    limit and took the (working) document downloads off the CNN with it."""
    import base64

    from tenders import captcha, captcha_model

    monkeypatch.setattr(captcha_model.TrainedSolver, "get",
                        classmethod(lambda cls, cfg: type("S", (), {
                            "predict": staticmethod(lambda img: "abc123")})()))
    monkeypatch.setattr(captcha, "_unverified_streak", 0)
    src = ("data:image/png;base64,"
           + base64.b64encode((FIX / "captcha_e6p78P.png").read_bytes()).decode())

    for _ in range(captcha._CNN_STREAK_LIMIT * 2):
        assert captcha.solve_image(src, tracked=False) == "abc123"
    assert captcha._unverified_streak == 0

    for _ in range(captcha._CNN_STREAK_LIMIT):
        assert captcha.solve_image(src) == "abc123"
    assert captcha._unverified_streak == captcha._CNN_STREAK_LIMIT
    # Tripped: the CNN is now bypassed for everyone, tracked or not — a broken
    # model is everybody's problem even though only the gate may declare it one.
    # The fallback is Tesseract, and it is local: this used to assert on a
    # ``claude -p`` vision shim, which was removed so that nothing on the
    # capture path can depend on an external service (see captcha's docstring).
    monkeypatch.setattr(captcha, "tesseract_available", lambda: True)
    monkeypatch.setattr(captcha, "_ocr_with", lambda img, psm: "fallback")
    assert captcha.solve_image(src, tracked=False) == "fallback"


def test_parse_boq_summary_comparative_statement():
    from tenders.enrich_awards import parse_award
    text = ("BOQ Summary Details\n"
            "Tender ID: 2025_RDTN_609574_1\n"
            "Sheet Name Sl.No Bidder Name Amount Bid Rank\n"
            "BoQ1 1 C Dhanapal (BID ID -1481671) 395305.50 L1\n"
            "2 T.Thirumalai (BID ID -1481676) 407572.24 L2\n")
    a = parse_award(text)
    assert a["awarded_to"] == "C Dhanapal (BID ID -1481671)"
    assert a["award_value"] == 395305.50
    assert [b["rank"] for b in a["bidders"]] == ["L1", "L2"]
    assert a["bidders"][1]["sheet"] == "BoQ1"


def test_parse_boq_summary_survives_glued_table_cells():
    """Narrow columns run together in the PDF text layer; the row still counts."""
    from tenders.enrich_awards import parse_award
    text = ("BOQ Summary Details\n"
            "Sheet Name Sl.No Bidder Name Amount Bid Rank\n"
            "BoQ1 1P RAYIN 19081458.28L1\n"
            "2P. RAYIN CONSTRUCTIONS COMPANY PVT LTD 19444997.40L2\n")
    a = parse_award(text)
    assert a["awarded_to"] == "P RAYIN"
    assert len(a["bidders"]) == 2


def test_parse_schedule_form_prefers_the_documents_own_verdict():
    from tenders.enrich_awards import parse_award
    text = ("Contract No: BRR.C.No.B4/1183/2023-24\n"
            "SCHEDULE OF WORK / ITEM(S)\n"
            "1.00RVS CONSTRUCTIONS(GSTN-33ABBFR0927P1Z0) 11854616.95 -1.90 11629379.22One Crore\n"
            "2.00u r subramanian(GSTN-NA) 11854616.95 12.50 13336444.06One Crore\n"
            "Lowest Amount Quoted BY: RVS CONSTRUCTIONS(11629379.22)\n")
    a = parse_award(text)
    assert a["awarded_to"] == "RVS CONSTRUCTIONS"
    assert a["award_value"] == 11629379.22
    assert a["award_ref"] == "BRR.C.No.B4/1183/2023-24"
    assert a["bidders"][0]["percent"] == -1.90


def test_free_text_award_letter_yields_no_winner():
    """Naming the wrong recipient of a public contract is worse than naming none."""
    from tenders.enrich_awards import parse_award
    a = parse_award("Madam,\n Sub: Work awarded to M/s. Sundar Travels - Regarding.\n")
    assert a["awarded_to"] is None and a["bidders"] == []


def test_multi_sheet_award_states_no_single_winner():
    from tenders.enrich_awards import parse_award
    text = ("BOQ Summary Details\n"
            "Sheet Name Sl.No Bidder Name Amount Bid Rank\n"
            "BoQ1 1 Alpha 100.00 L1\n"
            "BoQ2 1 Beta 200.00 L1\n")
    a = parse_award(text)
    assert a["awarded_to"] is None
    assert len(a["bidders"]) == 2


def test_contract_no_does_not_swallow_the_next_line():
    from tenders.enrich_awards import parse_award
    a = parse_award("SCHEDULE OF WORK / ITEM(S)\nContract No:\nSCHEDULE OF WORK\n")
    assert a["award_ref"] is None


def test_pdf_signature_date_is_read_as_ist_wall_clock():
    from tenders.enrich_awards import signature
    stamp, who = signature(rb"<< /M (D:20250918155107+05'30') /Name (JAYALASHMI G) >>")
    assert stamp == "2025-09-18T15:51:07"
    assert who == "JAYALASHMI G"
    assert signature(b"no signature here") == (None, None)


def test_status_list_permalinks_are_repaired_before_use():
    """component=view answers with a session-expired page, not the tender."""
    from tenders.pipeline import normalise_detail_url
    bad = ("https://tntenders.gov.in/nicgep/app?component=view"
           "&page=FrontEndViewTender&service=direct&sp=SD6f0hpWFzMgHaqDLULm1dQ%3D%3D")
    assert "component=%24DirectLink&" in normalise_detail_url(bad)
    good = bad.replace("component=view", "component=%24DirectLink")
    assert normalise_detail_url(good) == good
    # A listing URL that is not a detail permalink must be left alone.
    other = "https://tntenders.gov.in/nicgep/app?component=view&page=WebTenderStatusLists"
    assert normalise_detail_url(other) == other
    assert normalise_detail_url(None) is None


def test_empty_detail_parse_is_refused_rather_than_written():
    """A session-expired page is a 200; writing it would NULL a recovered record."""
    from tenders.pipeline import _rejection_reason
    assert _rejection_reason({}, "2025_X_1_1")
    assert _rejection_reason({"Tender ID": "2025_OTHER_9_9"}, "2025_X_1_1")
    assert _rejection_reason({"Tender ID": "2025_X_1_1"}, "2025_X_1_1") is None
    assert _rejection_reason({"Organisation Chain": "Dept"}, "2025_X_1_1") is None
