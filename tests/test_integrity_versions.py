"""Document versioning, integrity auditing and cancellation parsing.

These exercise the paths that decide whether an irreplaceable file survives, so
they run against a real SQLite database and real files on disk rather than
mocks — the failure modes being guarded against (a lost predecessor, a silently
truncated capture) only exist at that level.
"""

from pathlib import Path

import pytest

from tenders import config as config_mod
from tenders.db import connect, document_versions, init_db
from tenders.parse_detail import cancellation, parse_detail

CONFIG_TEMPLATE = """
[site]
base_url = "https://example.invalid/nicgep/app"
host = "https://example.invalid"
active_page = "https://example.invalid/a"
archive_page = "https://example.invalid/b"
active_org_page = "https://example.invalid/c"

[paths]
db = "{root}/data/tenders.db"
docs = "{root}/data/docs"
html = "{root}/data/html"
captcha = "{root}/data/captcha"

[scrape]
min_interval_s = 0.0
jitter_s = 0.0
timeout_s = 5
max_retries = 1
user_agent = "test"
max_requests_per_run = 0
captcha_attempts = 1
captcha_manual = false

[forward]
priority_window_days = 3

[integrity]
recheck_max_per_run = 40
recheck_max_per_tender = 5

[ocr]
enabled = false
min_chars_per_page = 40
max_ocr_pages = 1

[web]
host = "127.0.0.1"
port = 8000
page_size = 25
"""


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """An isolated config + database + docs tree."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(CONFIG_TEMPLATE.format(root=tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)
    config_mod.load_config.cache_clear()
    cfg = config_mod.load_config()
    cfg.ensure_dirs()
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    conn.execute(
        "INSERT INTO tenders (tender_id, first_seen_at, last_updated_at)"
        " VALUES ('2026_TEST_1_1', '2026-01-01T00:00:00+00:00',"
        " '2026-01-01T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO documents (tender_id, filename, section, status, source)"
        " VALUES ('2026_TEST_1_1', 'Tendernotice_1.pdf', 'NIT', 'pending', 'scraped')")
    conn.commit()
    doc_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
    yield cfg, conn, doc_id
    conn.close()
    config_mod.load_config.cache_clear()


def _pdf(body: bytes) -> bytes:
    return b"%PDF-1.4\n" + body + b"\ntrailer\n%%EOF\n"


def test_modification_preserves_the_previous_file(sandbox):
    from tenders.download_docs import store_document

    cfg, conn, doc_id = sandbox
    target = Path(cfg.docs_dir) / "2026_TEST_1_1" / "Tendernotice_1.pdf"
    first = _pdf(b"original specification")
    second = _pdf(b"quietly edited specification")

    assert store_document(conn, cfg, doc_id, target, first, "application/pdf") == "new"
    assert store_document(conn, cfg, doc_id, target, first, "application/pdf") == "unchanged"
    assert store_document(conn, cfg, doc_id, target, second, "application/pdf") == "modified"
    conn.commit()

    # The live path holds the new bytes; the old bytes still exist untouched.
    assert target.read_bytes() == second
    preserved = list((target.parent / "versions").glob("*Tendernotice_1.pdf"))
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == first

    chain = document_versions(conn, doc_id)
    assert len(chain) == 2
    assert chain[0]["current"] is True
    assert chain[0]["byte_size"] == len(second)
    assert chain[1]["current"] is False
    assert chain[1]["byte_size"] == len(first)
    assert chain[1]["superseded_at"]
    assert chain[0]["version_count"] == 2

    events = [r["event"] for r in conn.execute(
        "SELECT event FROM document_events WHERE document_id=? ORDER BY id", (doc_id,))]
    assert events == ["captured", "version_captured"]


def test_unchanged_bytes_are_not_rewritten(sandbox):
    from tenders.download_docs import store_document

    cfg, conn, doc_id = sandbox
    target = Path(cfg.docs_dir) / "2026_TEST_1_1" / "Tendernotice_1.pdf"
    content = _pdf(b"stable")
    store_document(conn, cfg, doc_id, target, content, "application/pdf")
    mtime = target.stat().st_mtime_ns
    store_document(conn, cfg, doc_id, target, content, "application/pdf")
    assert target.stat().st_mtime_ns == mtime
    assert not (target.parent / "versions").exists()


@pytest.mark.parametrize(
    "corrupt, reason",
    [
        (b"", "zero_bytes"),
        (b"%PDF-1.4\ntruncated mid-stream", "pdf_truncated"),
        (b"<html><body>Session expired</body></html>", "html_error_page"),
    ],
)
def test_integrity_detects_bad_captures_and_requeues(sandbox, corrupt, reason):
    from tenders.download_docs import store_document
    from tenders.integrity import run_verify

    cfg, conn, doc_id = sandbox
    target = Path(cfg.docs_dir) / "2026_TEST_1_1" / "Tendernotice_1.pdf"
    store_document(conn, cfg, doc_id, target, _pdf(b"good"), "application/pdf")
    conn.execute("UPDATE documents SET download_url='https://example.invalid/d'"
                 " WHERE id=?", (doc_id,))
    conn.commit()

    # Damage the file *and* resync the recorded size/hash to the damage. This
    # is the hard case and the one seen in production: the metadata agrees with
    # the bytes, so only a content-aware check can tell the capture is worthless.
    import hashlib

    target.write_bytes(corrupt)
    conn.execute("UPDATE documents SET byte_size=?, sha256=? WHERE id=?",
                 (len(corrupt), hashlib.sha256(corrupt).hexdigest(), doc_id))
    conn.commit()

    res = run_verify(cfg.db_path, limit=0, max_age_hours=0, progress=lambda _m: None)
    assert res["checked"] == 1 and res["invalid"] == 1
    assert reason in next(iter(res["reasons"]))

    row = conn.execute("SELECT status, sha256 FROM documents WHERE id=?",
                       (doc_id,)).fetchone()
    assert row["status"] == "pending"   # back in the capture queue
    assert row["sha256"] is None        # so the redownload counts as new, not modified
    assert target.exists()              # nothing under data/docs is ever removed


def test_integrity_leaves_valid_files_alone(sandbox):
    from tenders.download_docs import store_document
    from tenders.integrity import run_verify

    cfg, conn, doc_id = sandbox
    target = Path(cfg.docs_dir) / "2026_TEST_1_1" / "Tendernotice_1.pdf"
    store_document(conn, cfg, doc_id, target, _pdf(b"intact"), "application/pdf")
    conn.commit()
    res = run_verify(cfg.db_path, limit=0, max_age_hours=0, progress=lambda _m: None)
    assert res == {"checked": 1, "valid": 1, "invalid": 0, "requeued": 0,
                   "reasons": {}, "examples": []}
    assert conn.execute("SELECT status FROM documents WHERE id=?",
                        (doc_id,)).fetchone()["status"] == "captured"


CANCELLED_HTML = """
<html><body><table><tr><td>
  <table class="list_table" id="corrigendumDocumenttable">
    <tr class="list_header"><td>S.No</td><td>Corrigendum Title</td>
        <td>Corrigendum Type</td><td>View</td></tr>
    <tr><td>1</td><td>Lodging of Tender</td><td>Cancellation of Tender</td><td></td></tr>
    <tr><td>2</td><td>Due date extension</td><td>Date</td><td></td></tr>
  </table>
</td></tr></table>
<a href="/nicgep/app?page=WebCancelledTenderLists">Cancelled/Retendered</a>
</body></html>
"""


def test_cancellation_corrigendum_is_parsed():
    parsed = parse_detail(CANCELLED_HTML)
    types = [c["type"] for c in parsed["corrigenda"]]
    assert types == ["Cancellation of Tender", "Date"]
    # The site-navigation "Cancelled/Retendered" link must not be mistaken for one.
    assert cancellation(parsed["corrigenda"])["title"] == "Lodging of Tender"


def test_no_corrigenda_is_not_a_cancellation():
    parsed = parse_detail("<html><body><table></table></body></html>")
    assert parsed["corrigenda"] == []
    assert cancellation(parsed["corrigenda"]) is None
