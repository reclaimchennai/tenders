"""capture_retry.py — the frequent sweep that gets a new tender's documents
without waiting for forward_capture's much slower once-a-cycle pass.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tenders import capture_retry as CR
from tenders.config import Config
from tenders.db import init_db

# The due-check and the is_open check both live in raw SQL (datetime('now')),
# not behind an injectable clock, so — unlike test_watches.py's `now=` override
# — these have to be relative to the real wall clock.
NOW = datetime.now(timezone.utc)
PAST = (NOW - timedelta(minutes=5)).isoformat()
FUTURE = (NOW + timedelta(hours=6)).isoformat()


@pytest.fixture()
def cfg(tmp_path):
    db = tmp_path / "t.db"
    init_db(db)
    return Config(raw={
        "site": {}, "scrape": {}, "forward": {}, "ocr": {}, "web": {},
        "paths": {"db": str(db), "docs": str(tmp_path / "docs"),
                  "html": str(tmp_path / "html"), "captcha": str(tmp_path / "cap")},
        "capture_retry": {},
    })


@pytest.fixture()
def conn(cfg):
    c = sqlite3.connect(cfg.db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    yield c
    c.close()


def add_tender(conn, tid, *, status="discovered", closing=None,
               next_capture_at=PAST, capture_attempts=0,
               detail_url="https://host/tender"):
    conn.execute(
        "INSERT INTO tenders (tender_id, title, status, source, detail_url,"
        " closing_date, next_capture_at, capture_attempts,"
        " first_seen_at, last_updated_at)"
        " VALUES (?, 'T', ?, 'scraped', ?, ?, ?, ?, ?, ?)",
        (tid, status, detail_url, closing, next_capture_at, capture_attempts,
         NOW.isoformat(), NOW.isoformat()))
    conn.commit()


def add_pending_doc(conn, tid, status="pending"):
    conn.execute(
        "INSERT INTO documents (tender_id, filename, section, status)"
        " VALUES (?, 'f.pdf', 'Notice', ?)", (tid, status))
    conn.commit()


def _fake_fetch(outcomes):
    """A fetch_and_store_detail stand-in whose effect on the DB is scripted."""
    calls = []

    def fake(conn, client, cfg, tender_id, url, download=True):
        calls.append(tender_id)
        outcomes[tender_id](conn)
        return {"tender_id": tender_id, "ok": True}

    return fake, calls


def test_a_never_detailed_open_tender_due_now_is_captured(conn, cfg, monkeypatch):
    add_tender(conn, "T1", closing=FUTURE)

    def mark_detailed(c):
        c.execute("UPDATE tenders SET status='detailed' WHERE tender_id='T1'")

    fake, calls = _fake_fetch({"T1": mark_detailed})
    monkeypatch.setattr("tenders.pipeline.fetch_and_store_detail", fake)

    result = CR.run_due_captures(conn, object(), cfg, limit=10)
    assert calls == ["T1"]
    assert result == {"attempted": 1, "completed": 1}
    row = conn.execute("SELECT status, capture_attempts, next_capture_at"
                       " FROM tenders WHERE tender_id='T1'").fetchone()
    assert row["status"] == "detailed"
    # Done — schedule cleared, never asked again.
    assert row["capture_attempts"] == 0
    assert row["next_capture_at"] is None


def test_still_incomplete_after_a_probe_advances_to_the_next_schedule_step(
        conn, cfg, monkeypatch):
    """First probe fails to find a link yet: reschedule 5 minutes out (attempt 2)."""
    add_tender(conn, "T2", closing=FUTURE, capture_attempts=1)

    fake, calls = _fake_fetch({"T2": lambda c: None})  # detail read, still 'discovered'
    monkeypatch.setattr("tenders.pipeline.fetch_and_store_detail", fake)

    result = CR.run_due_captures(conn, object(), cfg, limit=10)
    assert calls == ["T2"]
    assert result == {"attempted": 1, "completed": 0}
    row = conn.execute("SELECT capture_attempts, next_capture_at"
                       " FROM tenders WHERE tender_id='T2'").fetchone()
    assert row["capture_attempts"] == 2
    due_in = (datetime.fromisoformat(row["next_capture_at"])
             - datetime.now(timezone.utc)).total_seconds()
    assert 250 < due_in < 320  # ~5 minutes, allowing test execution slack


def test_detailed_but_a_document_is_still_pending_stays_in_the_retry_pool(
        conn, cfg, monkeypatch):
    add_tender(conn, "T3", status="detailed", closing=FUTURE)
    add_pending_doc(conn, "T3")

    fake, calls = _fake_fetch({"T3": lambda c: None})  # link still not there
    monkeypatch.setattr("tenders.pipeline.fetch_and_store_detail", fake)

    result = CR.run_due_captures(conn, object(), cfg, limit=10)
    assert calls == ["T3"]
    assert result == {"attempted": 1, "completed": 0}


def test_a_tender_with_no_closing_date_is_never_picked_up(conn, cfg, monkeypatch):
    """NULL must not count as open here — see the module's is_open comment.

    This exact mistake once flooded forward_capture's unbounded live bucket
    with 62,334 undated rows; the fast path must not repeat it.
    """
    add_tender(conn, "UNDATED", closing=None)
    fake, calls = _fake_fetch({})
    monkeypatch.setattr("tenders.pipeline.fetch_and_store_detail", fake)

    result = CR.run_due_captures(conn, object(), cfg, limit=10)
    assert calls == []
    assert result == {"attempted": 0, "completed": 0}


def test_a_closed_tender_is_never_picked_up(conn, cfg, monkeypatch):
    add_tender(conn, "CLOSED", closing=(NOW - timedelta(days=1)).isoformat())
    fake, calls = _fake_fetch({})
    monkeypatch.setattr("tenders.pipeline.fetch_and_store_detail", fake)

    result = CR.run_due_captures(conn, object(), cfg, limit=10)
    assert calls == []
    assert result == {"attempted": 0, "completed": 0}


def test_a_tender_not_yet_due_is_left_alone(conn, cfg, monkeypatch):
    add_tender(conn, "NOTYET", closing=FUTURE, next_capture_at=FUTURE)
    fake, calls = _fake_fetch({})
    monkeypatch.setattr("tenders.pipeline.fetch_and_store_detail", fake)

    result = CR.run_due_captures(conn, object(), cfg, limit=10)
    assert calls == []
    assert result == {"attempted": 0, "completed": 0}


def test_batch_size_bounds_one_sweep_but_nothing_is_lost(conn, cfg, monkeypatch):
    for i in range(5):
        add_tender(conn, f"BATCH{i}", closing=FUTURE)
    fake, calls = _fake_fetch({f"BATCH{i}": (lambda c: None) for i in range(5)})
    monkeypatch.setattr("tenders.pipeline.fetch_and_store_detail", fake)

    first = CR.run_due_captures(conn, object(), cfg, limit=2)
    assert first["attempted"] == 2
    # The other three are still due (next_capture_at unchanged for them) and
    # are first in line on the next sweep.
    second = CR.run_due_captures(conn, object(), cfg, limit=10)
    assert second["attempted"] == 3
