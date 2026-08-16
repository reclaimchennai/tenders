"""Guards on the one thing that can destroy this feature: notifying about history.

The archive is still recovering 2020-2026 tenders at tens of thousands of rows a
day, and the award sweep is attaching 2025 contract awards to tenders nobody has
ever been told about. Both of those are, to a naive detector, indistinguishable
from news. The tests below are written as the disasters themselves — a
20,000-row backfill landing, a 5,000-tender award sweep running — and assert
that the number of notifications is exactly zero.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tenders.config import Config
from tenders.db import init_db
from tenders import push as push_mod
from tenders import watches as W


@pytest.fixture()
def cfg(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    return Config(raw={
        "site": {}, "scrape": {}, "forward": {}, "ocr": {}, "web": {},
        "paths": {"db": str(db), "docs": str(tmp_path / "docs"),
                  "html": str(tmp_path / "html"), "captcha": str(tmp_path / "cap")},
        "push": {"vapid_key_file": str(tmp_path / "vapid.pem"), "subject": "mailto:t@e"},
        "watch": {},
    })


@pytest.fixture()
def conn(cfg):
    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


NOW = datetime(2026, 8, 9, 12, 0, 0)


def add_tender(conn, tid, *, published=None, title="Road widening work",
               org="Highways Department||Chennai", status="detailed",
               closing="2026-09-01T15:00:00", corrigenda=0, awarded=None,
               cancelled=None, value=500000.0):
    conn.execute(
        "INSERT INTO tenders (tender_id, title, work_description, organisation_chain,"
        " location, closing_date, published_date, tender_category, tender_value_num,"
        " raw_json, source, status, corrigendum_count, awarded_at, cancelled_at,"
        " first_seen_at, last_updated_at) VALUES (?,?,?,?,?,?,?,?,?,'{}','scraped',?,?,?,?,?,?)",
        (tid, title, title, org, "Chennai", closing, published, "Works", value,
         status, corrigenda, awarded, cancelled, NOW.isoformat(), NOW.isoformat()))
    conn.execute(
        "INSERT INTO tenders_fts (tender_id, title, work_description,"
        " organisation_chain, location, reference_number) VALUES (?,?,?,?,?,'')",
        (tid, title, title, org, "Chennai"))
    conn.commit()


def subscribe(conn, endpoint="https://push.example/abc123"):
    return W.upsert_subscription(conn, endpoint, "p256dh-key", "auth-key", "android")


def watch(conn, sub_id, filters="q=road", *, created: datetime = NOW):
    w = W.add_watch(conn, sub_id, filters)
    conn.execute("UPDATE watches SET cutoff_at=? WHERE id=?",
                 (created.isoformat(), w["id"]))
    conn.commit()
    return dict(conn.execute("SELECT * FROM watches WHERE id=?", (w["id"],)).fetchone())


def run(cfg, **kw):
    return W.run_watches(cfg.db_path, cfg=cfg, dry_run=True, now=NOW, **kw)


def _capture(monkeypatch) -> list[dict]:
    """Every payload a pass would put on the wire, in order.

    A dry run never reaches push.send, so anything asserted about what a
    notification actually *says* — its tag, its url, its body — has to go through
    a live pass with the transport replaced.
    """
    sent: list[dict] = []

    def fake_send(_vapid, _sub, payload, topic=None, **_kw):
        sent.append(dict(payload, topic=topic))
        return push_mod.classify(201)

    monkeypatch.setattr(push_mod, "load_vapid",
                        lambda _cfg: push_mod.Vapid("pem", "pub", "mailto:t@e"))
    monkeypatch.setattr(push_mod, "send", fake_send)
    return sent


def run_live(cfg, **kw):
    return W.run_watches(cfg.db_path, cfg=cfg, now=NOW, **kw)


# ---------------------------------------------------------------------------
# The disaster this feature is most likely to cause
# ---------------------------------------------------------------------------

def test_historical_backfill_of_20000_tenders_notifies_nobody(conn, cfg):
    """A status-listing backfill landing at 3am must produce zero pushes.

    Modelled on what the live backfills actually insert: overwhelmingly rows
    with no published_date at all, plus a minority that carry a real but old
    publication date. Every one of them matches the watch's filters.
    """
    sub = subscribe(conn)
    watch(conn, sub, "q=road", created=NOW - timedelta(hours=1))
    for i in range(18_000):
        add_tender(conn, f"HIST_{i}", published=None, status="discovered")
    for i in range(2_000):
        old = (NOW - timedelta(days=400 + i % 900)).isoformat()
        add_tender(conn, f"OLD_{i}", published=old)

    result = run(cfg)
    assert result["matches"] == 0
    assert result["pushes"] == 0


def test_award_sweep_over_5000_bookmarked_tenders_alerts_nobody(conn, cfg):
    """The retrospective award sweep must not announce 2025 contracts in 2026.

    Every tender here is bookmarked *and already detailed* when the alert is
    registered — the strongest case for the detector, with no "we had not read
    the detail page" excuse available — and then acquires an award exactly the
    way enrich_awards fills one in.
    """
    sub = subscribe(conn)
    for i in range(5_000):
        add_tender(conn, f"AW_{i}", published=(NOW - timedelta(days=300)).isoformat())
        W.add_alert(conn, sub, f"AW_{i}")
    aoc = (NOW - timedelta(days=250)).isoformat()
    conn.execute("UPDATE tenders SET awarded_at=?, awarded_to='Some Contractor',"
                 " award_value_num=990000.0 WHERE tender_id LIKE 'AW_%'", (aoc,))
    conn.commit()

    result = run(cfg)
    assert result["changes"] == 0
    assert result["pushes"] == 0


def test_first_detail_pass_on_a_stub_is_absorbed_silently(conn, cfg):
    """Learning a tender's whole history at once is catching up, not news.

    A bookmarked stub the archive had never detailed acquires, in one pass, four
    corrigenda, a 2023 cancellation and a closing date. None of that is a change
    the portal made while the user was watching.
    """
    sub = subscribe(conn)
    add_tender(conn, "STUB", published=(NOW - timedelta(days=2)).isoformat(),
               status="discovered", closing=None, corrigenda=0)
    W.add_alert(conn, sub, "STUB")
    conn.execute(
        "UPDATE tenders SET status='detailed', corrigendum_count=4,"
        " corrigenda_json='[{\"type\":\"Date\"}]', cancelled_at='2023-04-01T00:00:00',"
        " closing_date='2026-09-01T15:00:00' WHERE tender_id='STUB'")
    conn.commit()

    assert run(cfg)["changes"] == 0
    # ...and the baseline moved, so it is never re-examined.
    row = conn.execute("SELECT * FROM tender_alerts WHERE tender_id='STUB'").fetchone()
    assert row["base_status"] == "detailed"
    assert row["base_corrigendum_count"] == 4


# ---------------------------------------------------------------------------
# ...while still doing its job
# ---------------------------------------------------------------------------

def test_a_genuinely_new_publication_notifies_exactly_once(conn, cfg):
    sub = subscribe(conn)
    watch(conn, sub, "q=road", created=NOW - timedelta(hours=6))
    add_tender(conn, "NEW1", published=(NOW - timedelta(hours=2)).isoformat())

    first = run(cfg)
    assert first["matches"] == 1 and first["pushes"] == 1
    # The rate cap is bypassed only because nothing new arrived; the exactly-once
    # record is what has to hold.
    conn.execute("UPDATE watches SET last_notified_at=NULL")
    conn.commit()
    assert run(cfg)["matches"] == 0


def test_undetailed_tender_does_not_notify_until_its_documents_are_read(conn, cfg):
    """A published_date alone is not enough — the detail pass must have run.

    published_date is known from the fast "latest" listing poll, minutes
    after the portal posts a tender and typically well before the slower
    detail+download pass reaches it. Notifying at that point sends a user to
    a tender page with no documents on it, while the real portal already
    shows them. The match must wait for status='detailed', and must not be
    lost while it waits.
    """
    sub = subscribe(conn)
    watch(conn, sub, "q=road", created=NOW - timedelta(hours=6))
    add_tender(conn, "PENDING1", published=(NOW - timedelta(hours=2)).isoformat(),
               status="discovered")

    first = run(cfg)
    assert first["matches"] == 0 and first["pushes"] == 0

    conn.execute("UPDATE tenders SET status='detailed' WHERE tender_id='PENDING1'")
    conn.commit()

    second = run(cfg)
    assert second["matches"] == 1 and second["pushes"] == 1


def test_a_watch_never_speaks_about_the_past(conn, cfg):
    """Published an hour before the watch existed — the watch says nothing."""
    sub = subscribe(conn)
    watch(conn, sub, "q=road", created=NOW - timedelta(hours=1))
    add_tender(conn, "BEFORE", published=(NOW - timedelta(hours=2)).isoformat())
    assert run(cfg)["matches"] == 0


def test_freshness_floor_binds_even_on_an_old_watch(conn, cfg):
    """A watch created a year ago still cannot be told about a year-old tender.

    This is the layer that survives a row *acquiring* an old published_date
    during a later detail pass, long after the watch's own cutoff stopped
    excluding anything.
    """
    sub = subscribe(conn)
    watch(conn, sub, "q=road", created=NOW - timedelta(days=365))
    add_tender(conn, "STALE", published=(NOW - timedelta(days=30)).isoformat())
    assert run(cfg)["matches"] == 0


def test_a_new_corrigendum_on_a_known_tender_is_news(conn, cfg):
    sub = subscribe(conn)
    add_tender(conn, "LIVE", published=(NOW - timedelta(days=3)).isoformat(),
               corrigenda=1)
    W.add_alert(conn, sub, "LIVE")
    conn.execute("UPDATE tenders SET corrigendum_count=2,"
                 " corrigenda_json='[{\"type\":\"Date\"},{\"type\":\"BOQ\"}]'"
                 " WHERE tender_id='LIVE'")
    conn.commit()
    assert run(cfg)["changes"] == 1


def test_a_recent_award_is_news(conn, cfg):
    sub = subscribe(conn)
    add_tender(conn, "AOC", published=(NOW - timedelta(days=60)).isoformat())
    W.add_alert(conn, sub, "AOC")
    conn.execute("UPDATE tenders SET awarded_at=?, awarded_to='Acme Ltd',"
                 " award_value_num=1.0 WHERE tender_id='AOC'",
                 ((NOW - timedelta(days=2)).isoformat(),))
    conn.commit()
    assert run(cfg)["changes"] == 1


def test_a_republished_document_is_news(conn, cfg):
    """The quiet mid-tender edit this archive exists to catch."""
    sub = subscribe(conn)
    add_tender(conn, "DOC", published=(NOW - timedelta(days=3)).isoformat())
    W.add_alert(conn, sub, "DOC")
    conn.execute(
        "INSERT INTO document_events (tender_id, event, at) VALUES"
        " ('DOC','version_captured',?)",
        ((datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),))
    conn.commit()
    assert run(cfg)["changes"] == 1


def test_several_changed_bookmarks_are_one_push(conn, cfg):
    """Three changed bookmarks must not be three notifications."""
    sub = subscribe(conn)
    for i in range(3):
        add_tender(conn, f"B{i}", published=(NOW - timedelta(days=3)).isoformat(),
                   corrigenda=1)
        W.add_alert(conn, sub, f"B{i}")
    conn.execute("UPDATE tenders SET corrigendum_count=2 WHERE tender_id LIKE 'B%'")
    conn.commit()
    result = run(cfg)
    assert result["changes"] == 3
    assert result["pushes"] == 1


def test_fifty_matches_become_four_tenders_and_a_count(conn, cfg):
    """A flood is still capped — it just arrives readable rather than as one line.

    The old contract was one batched push per watch. The new one is one push per
    tender, which is only safe because the ceiling moved with it: 50 matches must
    produce five notifications, never fifty.
    """
    sub = subscribe(conn)
    watch(conn, sub, "q=road", created=NOW - timedelta(hours=6))
    for i in range(50):
        add_tender(conn, f"M{i}", published=(NOW - timedelta(hours=1)).isoformat())
    result = run(cfg)
    assert result["matches"] == 50
    assert result["pushes"] == W.DEFAULTS["max_tender_pushes"] + 1


def test_each_match_gets_its_own_tag_and_its_own_tender_url(conn, cfg, monkeypatch):
    """The whole point of the split: separately dismissible, separately openable.

    A shared tag is what made Android replace the previous notification instead
    of stacking beside it, and a shared /browse url is what made every one of
    them land on a result page the reader then had to search again.
    """
    sent = _capture(monkeypatch)
    sub = subscribe(conn)
    watch(conn, sub, "q=road", created=NOW - timedelta(hours=6))
    for i in range(3):
        add_tender(conn, f"N{i}", published=(NOW - timedelta(hours=1)).isoformat())
    run_live(cfg)

    assert len(sent) == 3
    assert sorted(p["tag"] for p in sent) == ["tender-N0", "tender-N1", "tender-N2"]
    assert sorted(p["url"] for p in sent) == ["/tender/N0", "/tender/N1", "/tender/N2"]
    # Distinct topics too: the push service collapses undelivered messages that
    # share one, so a reused topic would undo the split at the transport layer.
    assert len({p["topic"] for p in sent}) == 3
    for payload in sent:
        # Headline in the title, context underneath it — not a joined list.
        assert "\n" not in payload["title"]
        assert "Road widening work" in payload["title"]
        # The office, then the deadline, then why this arrived — same choice
        # describe() makes: the tail of the chain is the name people know.
        assert payload["body"].startswith("Chennai\nCloses ")
        assert payload["body"].endswith("Alert: “road”")


def test_the_overflow_summary_points_at_the_search(conn, cfg, monkeypatch):
    sent = _capture(monkeypatch)
    sub = subscribe(conn)
    watch(conn, sub, "q=road", created=NOW - timedelta(hours=6))
    for i in range(7):
        add_tender(conn, f"O{i}", published=(NOW - timedelta(hours=1)).isoformat())
    run_live(cfg)

    cap = W.DEFAULTS["max_tender_pushes"]
    assert len(sent) == cap + 1
    summary = sent[-1]
    assert summary["url"] == "/browse?q=road"
    assert summary["title"].startswith(f"{7 - cap} more new tenders")
    # And the individual ones did not also claim the leftovers.
    assert sum(p["count"] for p in sent) == 7


def test_a_device_is_never_sent_more_than_its_ceiling_in_one_pass(conn, cfg, monkeypatch):
    sent = _capture(monkeypatch)
    sub = subscribe(conn)
    for n, qs in enumerate(("q=road", "q=drain", "q=culvert")):
        watch(conn, sub, qs, created=NOW - timedelta(hours=6))
        for i in range(6):
            add_tender(conn, f"C{n}{i}", title=qs.split("=")[1] + " work",
                       published=(NOW - timedelta(hours=1)).isoformat())
    result = run_live(cfg)
    assert len(sent) == W.DEFAULTS["max_pushes_per_subscription"]
    assert result["deferred"] > 0


def _spread_matches(conn, cfg, *, per_watch, ceiling):
    """One watch with five matches spread over five days, under given caps."""
    cfg.raw["watch"] = {"max_tender_pushes": per_watch,
                        "max_pushes_per_subscription": ceiling}
    sub = subscribe(conn)
    w = watch(conn, sub, "q=road", created=NOW - timedelta(days=6))
    for i in range(5):
        add_tender(conn, f"H{i}", published=(NOW - timedelta(days=i)).isoformat())
    run(cfg)
    row = conn.execute("SELECT hwm_published FROM watches WHERE id=?",
                       (w["id"],)).fetchone()
    notified = {r[0] for r in conn.execute(
        "SELECT tender_id FROM watch_matches WHERE watch_id=?", (w["id"],))}
    return row[0], notified


def test_a_watch_cut_by_the_ceiling_does_not_move_its_high_water_mark(conn, cfg):
    """A mark that ran ahead of what was announced would lose the remainder.

    ``hwm_published`` raises the floor of the next pass. Advancing it to the
    newest match while the ceiling was still holding the older ones back would
    drop those below their own floor and they would never be announced at all.
    """
    hwm, notified = _spread_matches(conn, cfg, per_watch=2, ceiling=2)
    # Two individual notifications went out; the "…and 3 more" summary did not.
    assert notified == {"H0", "H1"}
    assert not hwm


def test_a_watch_that_said_everything_does_move_its_high_water_mark(conn, cfg):
    hwm, notified = _spread_matches(conn, cfg, per_watch=2, ceiling=8)
    assert notified == {f"H{i}" for i in range(5)}
    assert hwm.startswith(NOW.date().isoformat())


# ---------------------------------------------------------------------------
# A watch must be the same question /browse answers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("qs", [
    "q=road",
    "q=drain&org=Highways+Department",
    "org=Highways+Department&category=Works",
    "value_min=100000&value_max=900000",
    "q=road&criteria=emd_exempt",
    "tender_type=Open+Tender&page=4&scope=all",
])
def test_watch_filters_answer_the_same_question_as_browse(conn, cfg, qs, monkeypatch):
    """A watch that returned different results from /browse would be a bug.

    Asserted against the real endpoint rather than against a second reading of
    its signature, because the failure this guards against is precisely the two
    drifting apart.
    """
    from fastapi.testclient import TestClient
    import tenders.web.app as web_app
    from tenders.db import ThreadLocalReader

    for i in range(12):
        add_tender(conn, f"E{i}",
                   published=(NOW - timedelta(days=i)).isoformat(),
                   title="Road and drain widening" if i % 2 else "Bridge repair",
                   value=200000.0 * (i + 1),
                   org="Highways Department||Chennai" if i % 3 else "Health Department")

    # The endpoint is served on a threadpool thread, and a sqlite3 connection
    # belongs to the thread that made it — the same reason web.app uses this
    # class in production rather than one shared handle.
    monkeypatch.setattr(web_app, "_reader", ThreadLocalReader(cfg.db_path))
    client = TestClient(web_app.app)
    html = client.get("/browse?" + qs + "&partial=1").text
    from_browse = sorted(set(__import__("re").findall(r'/tender/(E\d+)', html)))

    parsed = W.parse_filters(qs)
    from tenders.web.search import build_match, search_tenders_advanced

    rows, _ = search_tenders_advanced(
        conn, build_match(parsed["q"]), criteria=tuple(parsed["criteria"]),
        limit=25, offset=0, **parsed["filt"])
    from_watch = sorted({r["tender_id"] for r in rows})
    assert from_watch == from_browse


def test_canonical_filters_collapse_equivalent_routes():
    """Two routes to one search must be one watch, not two that both fire."""
    a = W.canonical_filters("q=road&org=Highways&category=Works&page=2")
    b = W.canonical_filters("category=Works&org=Highways&q=road&scope=all")
    assert a == b
    # ...and a filter that changes the answer must not collapse into it.
    assert W.canonical_filters("q=road&org=Health") != a


# ---------------------------------------------------------------------------
# Dead subscriptions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,action", [
    (404, "delete"), (410, "delete"), (429, "backoff"),
    (201, "keep"), (500, "fail"), (400, "fail"),
])
def test_push_status_is_classified(status, action):
    assert push_mod.classify(status, "300").action == action


def test_a_410_deletes_the_subscription_and_its_watches(conn, cfg, monkeypatch):
    """A push service that says the subscription is gone is believed, once."""
    sub = subscribe(conn)
    watch(conn, sub, "q=road", created=NOW - timedelta(hours=6))
    W.add_alert(conn, sub, "GONE") if False else None
    add_tender(conn, "N1", published=(NOW - timedelta(hours=1)).isoformat())

    monkeypatch.setattr(push_mod, "load_vapid",
                        lambda _cfg: push_mod.Vapid("pem", "pub", "mailto:t@e"))
    monkeypatch.setattr(push_mod, "send",
                        lambda *a, **k: push_mod.classify(410))
    result = W.run_watches(cfg.db_path, cfg=cfg, now=NOW)
    assert result["dropped"] == 1
    assert conn.execute("SELECT count(*) FROM push_subscriptions").fetchone()[0] == 0
    # The cascade is what makes deletion real.
    assert conn.execute("SELECT count(*) FROM watches").fetchone()[0] == 0


def test_repeated_soft_failures_eventually_drop_the_subscription(conn, cfg, monkeypatch):
    sub = subscribe(conn)
    watch(conn, sub, "q=road", created=NOW - timedelta(hours=6))
    add_tender(conn, "N1", published=(NOW - timedelta(hours=1)).isoformat())
    monkeypatch.setattr(push_mod, "load_vapid",
                        lambda _cfg: push_mod.Vapid("pem", "pub", "mailto:t@e"))
    monkeypatch.setattr(push_mod, "send", lambda *a, **k: push_mod.classify(500))
    for _ in range(push_mod.MAX_FAILURES):
        conn.execute("UPDATE watches SET last_notified_at=NULL")
        conn.commit()
        W.run_watches(cfg.db_path, cfg=cfg, now=NOW)
    assert conn.execute("SELECT count(*) FROM push_subscriptions").fetchone()[0] == 0


def test_a_429_backs_off_instead_of_hammering(conn, cfg, monkeypatch):
    sub = subscribe(conn)
    watch(conn, sub, "q=road", created=NOW - timedelta(hours=6))
    add_tender(conn, "N1", published=(NOW - timedelta(hours=1)).isoformat())
    monkeypatch.setattr(push_mod, "load_vapid",
                        lambda _cfg: push_mod.Vapid("pem", "pub", "mailto:t@e"))
    monkeypatch.setattr(push_mod, "send", lambda *a, **k: push_mod.classify(429, "600"))
    W.run_watches(cfg.db_path, cfg=cfg, now=NOW)
    row = conn.execute("SELECT retry_after FROM push_subscriptions").fetchone()
    assert row["retry_after"] is not None
    # And nothing was recorded as delivered, so the matches are still pending.
    assert conn.execute("SELECT count(*) FROM watch_matches").fetchone()[0] == 0


def test_forget_deletes_everything_for_a_device(conn, cfg):
    endpoint = "https://push.example/zzz"
    sub = W.upsert_subscription(conn, endpoint, "k", "a", "android")
    watch(conn, sub, "q=road")
    add_tender(conn, "T", published=NOW.isoformat())
    W.add_alert(conn, sub, "T")
    assert W.forget(conn, endpoint)["deleted"] == 1
    for table in ("push_subscriptions", "watches", "tender_alerts", "watch_matches"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_endpoints_are_never_logged(conn, cfg, monkeypatch, caplog):
    """A push endpoint in a log file is a privacy leak that outlives the row."""
    endpoint = "https://push.example/SECRET-TOKEN-9f3a"
    sub = W.upsert_subscription(conn, endpoint, "k", "a", "android")
    watch(conn, sub, "q=road", created=NOW - timedelta(hours=6))
    add_tender(conn, "N1", published=(NOW - timedelta(hours=1)).isoformat())
    monkeypatch.setattr(push_mod, "load_vapid",
                        lambda _cfg: push_mod.Vapid("pem", "pub", "mailto:t@e"))
    monkeypatch.setattr(push_mod, "send", lambda *a, **k: push_mod.classify(410))
    with caplog.at_level("DEBUG"):
        W.run_watches(cfg.db_path, cfg=cfg, now=NOW)
    assert "SECRET-TOKEN" not in caplog.text
    assert "push.example" in caplog.text  # the provider host still is, on purpose
