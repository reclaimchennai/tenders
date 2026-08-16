"""SQLite storage: schema bootstrap, connections, and FTS5 helpers.

The database is the single source of truth *and* the work queue. Tender
processing advances by ``status`` (discovered -> detailed -> failed) and
documents by their own ``status`` (pending -> captured | lost | failed).

Design notes:
* A small set of normalized, typed columns power search/browse/display; the
  complete set of scraped fields is preserved verbatim in ``raw_json`` so we
  never lose data even if the portal adds fields.
* FTS5 external-content tables mirror ``tenders`` and ``doc_text``; we keep them
  in sync explicitly after each batch (simpler than triggers during bulk loads).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger("db")

SCHEMA = r"""
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- One row per tender. Typed columns are normalized for search/display;
-- raw_json holds every scraped/imported field verbatim.
CREATE TABLE IF NOT EXISTS tenders (
    tender_id           TEXT PRIMARY KEY,
    reference_number    TEXT,
    title               TEXT,
    work_description    TEXT,
    organisation_chain  TEXT,
    tender_category     TEXT,
    tender_type         TEXT,
    product_category    TEXT,
    location            TEXT,
    pincode             TEXT,
    tender_value_raw    TEXT,
    tender_value_num    REAL,
    emd_raw             TEXT,
    tender_fee_raw      TEXT,
    published_date      TEXT,   -- ISO 8601
    published_date_raw  TEXT,
    opening_date        TEXT,
    opening_date_raw    TEXT,
    closing_date        TEXT,   -- bid submission end / closing, ISO 8601
    closing_date_raw    TEXT,
    detail_url          TEXT,   -- stable FrontEndViewTender permalink
    sp_token            TEXT,
    source              TEXT NOT NULL DEFAULT 'scraped',  -- 'csv' | 'scraped'
    status              TEXT NOT NULL DEFAULT 'discovered', -- discovered|detailed|failed
    raw_json            TEXT,
    detail_html_path    TEXT,
    first_seen_at       TEXT NOT NULL,
    last_updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tenders_status ON tenders(status);
CREATE INDEX IF NOT EXISTS idx_tenders_closing ON tenders(closing_date);
CREATE INDEX IF NOT EXISTS idx_tenders_org ON tenders(organisation_chain);

-- One row per document attached to a tender.
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_id     TEXT NOT NULL REFERENCES tenders(tender_id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    section       TEXT,           -- NIT / Work Item / BOQ / Pre-Bid / LoA ...
    description   TEXT,
    declared_size TEXT,
    download_url  TEXT,           -- present only while the file is live on the portal
    stored_path   TEXT,          -- local path once captured
    byte_size     INTEGER,
    sha256        TEXT,
    content_type  TEXT,
    status        TEXT NOT NULL DEFAULT 'pending', -- pending|captured|lost|failed
    source        TEXT NOT NULL DEFAULT 'scraped', -- 'csv' | 'scraped'
    downloaded_at TEXT,
    UNIQUE(tender_id, filename, section)
);
CREATE INDEX IF NOT EXISTS idx_docs_tender ON documents(tender_id);
CREATE INDEX IF NOT EXISTS idx_docs_status ON documents(status);

-- Superseded copies of a document. A tender's specification quietly edited
-- mid-bid is exactly the kind of change this archive exists to preserve, so a
-- re-published file never overwrites its predecessor: the old bytes are copied
-- aside under data/docs/<tender_id>/versions/ and recorded here, and the
-- `documents` row always describes the newest version.
CREATE TABLE IF NOT EXISTS document_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tender_id     TEXT,
    filename      TEXT,
    sha256        TEXT,
    byte_size     INTEGER,
    stored_path   TEXT,          -- the preserved copy, never the live path
    content_type  TEXT,
    captured_at   TEXT,          -- when this version was first downloaded
    superseded_at TEXT,          -- when a different sha256 replaced it
    UNIQUE(document_id, sha256)
);
CREATE INDEX IF NOT EXISTS idx_docver_doc ON document_versions(document_id);

-- Append-only capture-lifecycle log. Only *transitions* are written (never a
-- row per observation), because the point is to make answerable a question the
-- current schema throws away: how long after a tender is published do its
-- documents actually appear on the portal. Until that is measured the retry
-- schedule is a fixed guess; see doc_lifecycle.lag_report.
CREATE TABLE IF NOT EXISTS document_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    tender_id   TEXT,
    event       TEXT NOT NULL,  -- seen_missing|link_appeared|captured|
                                -- download_failed|version_captured|integrity_failed
    detail      TEXT,
    at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docev_doc ON document_events(document_id, at);
CREATE INDEX IF NOT EXISTS idx_docev_event ON document_events(event, at);

-- Extracted text per document (for full-text search inside files).
CREATE TABLE IF NOT EXISTS doc_text (
    document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    text        TEXT,
    method      TEXT,    -- pdf_layer | ocr | xlsx | xls | unsupported | error
    char_count  INTEGER,
    extracted_at TEXT
);

-- Resumable enumeration cursors.
CREATE TABLE IF NOT EXISTS crawl_state (
    listing      TEXT PRIMARY KEY,  -- 'active' | 'archive' | slice key
    cursor       TEXT,
    last_tender_id TEXT,
    page         INTEGER DEFAULT 0,
    complete     INTEGER DEFAULT 0,
    updated_at   TEXT
);

-- Procurement red flags (e.g. abnormally short bidding windows). One row per
-- tender per reason; detected_at is preserved from first detection.
CREATE TABLE IF NOT EXISTS redflags (
    tender_id    TEXT NOT NULL REFERENCES tenders(tender_id) ON DELETE CASCADE,
    reason       TEXT NOT NULL,        -- 'short_bid_window'
    severity     TEXT,                 -- 'high' | 'medium'
    window_hours REAL,
    published_at TEXT,
    closing_at   TEXT,
    detail       TEXT,
    detected_at  TEXT NOT NULL,
    PRIMARY KEY (tender_id, reason)
);
CREATE INDEX IF NOT EXISTS idx_redflags_detected ON redflags(detected_at);

-- Provenance: every fetch logged.
CREATE TABLE IF NOT EXISTS fetch_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT,
    tender_id   TEXT,
    http_status INTEGER,
    kind        TEXT,   -- detail | listing | document | captcha
    html_path   TEXT,
    fetched_at  TEXT
);

-- Web Push subscription: one row per browser that has granted permission.
-- These three strings *are* the credential — anyone holding them can push to
-- that browser — and the endpoint additionally identifies a person's device to
-- their push provider. So this table is the whole of what we know about a
-- subscriber: no account, no email, no IP, no user agent beyond a coarse
-- platform hint used only to explain the iOS install requirement back to them.
-- Deleting the row is a real deletion; the cascades below leave nothing behind.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint     TEXT NOT NULL UNIQUE,
    p256dh       TEXT NOT NULL,
    auth         TEXT NOT NULL,
    platform     TEXT,           -- 'android' | 'ios' | 'desktop' | NULL
    created_at   TEXT NOT NULL,
    last_seen_at TEXT,           -- refreshed whenever the browser re-registers
    last_push_at TEXT,
    retry_after  TEXT,           -- set from a 429; no push is attempted before it
    failures     INTEGER NOT NULL DEFAULT 0
);

-- A saved search. `filters` is the /browse querystring verbatim, never a frozen
-- result set: a watch is a live query, so replaying it is literally
-- GET /browse?<filters> and it keeps answering correctly as the archive grows.
--
-- cutoff_at is the rule that makes a watch honest. It is portal-local (IST)
-- wall-clock at the moment the watch was created, compared against
-- tenders.published_date, which is stored in the same frame. A watch can
-- therefore never notify about a tender the portal published before the watch
-- existed — however long afterwards this archive happens to discover it.
CREATE TABLE IF NOT EXISTS watches (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id  INTEGER NOT NULL REFERENCES push_subscriptions(id) ON DELETE CASCADE,
    filters          TEXT NOT NULL,
    label            TEXT NOT NULL,
    created_at       TEXT NOT NULL,   -- UTC, bookkeeping only
    cutoff_at        TEXT NOT NULL,   -- IST-naive, compared to published_date
    hwm_published    TEXT,            -- highest published_date already notified
    last_checked_at  TEXT,
    last_notified_at TEXT,
    notified_count   INTEGER NOT NULL DEFAULT 0,
    active           INTEGER NOT NULL DEFAULT 1,
    UNIQUE(subscription_id, filters)
);
CREATE INDEX IF NOT EXISTS idx_watches_sub ON watches(subscription_id);

-- Exactly-once bookkeeping: a tender already announced to a watch is never
-- announced to it again. This is deliberately a recorded fact and not a
-- position cursor, because publication order and discovery order are different
-- orders here — a tender published on Tuesday can land in this archive on
-- Friday, after Wednesday's has already been notified — and a cursor would
-- silently swallow it. Pruned by age; see watches.prune.
CREATE TABLE IF NOT EXISTS watch_matches (
    watch_id    INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    tender_id   TEXT NOT NULL,
    notified_at TEXT NOT NULL,
    PRIMARY KEY (watch_id, tender_id)
) WITHOUT ROWID;

-- Alerts on one specific tender ("tell me when this changes"), the second
-- subscription type over the same push pipeline as `watches`.
--
-- /bookmarks stays client-side localStorage, so this archive still does not
-- know what anyone has saved. A row appears here only when a user explicitly
-- turns alerts on for one tender, which is the only thing push can be built
-- from — a server cannot notify about a list it cannot see.
--
-- The base_* columns are the tender's change-relevant state *as it stood when
-- the user asked*, and they are the whole defence against announcing history.
-- The archive is still recovering 2020-2026 tenders, so a bookmarked tender
-- routinely acquires an award, a corrigendum list or a cancellation that the
-- portal published years ago; measured against this baseline that is a value we
-- had not read yet, not a value that changed. The baseline is advanced on every
-- pass whether or not anything was sent, so catching up is absorbed silently
-- and can only ever be absorbed once.
CREATE TABLE IF NOT EXISTS tender_alerts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id       INTEGER NOT NULL REFERENCES push_subscriptions(id) ON DELETE CASCADE,
    tender_id             TEXT NOT NULL,
    registered_at         TEXT NOT NULL,   -- UTC; nothing observed before this fires
    baseline_at           TEXT,            -- UTC; when base_* was last refreshed
    base_status           TEXT,            -- 'discovered' means we had not read the detail page
    base_corrigendum_count INTEGER,
    base_cancelled_at     TEXT,
    base_awarded_at       TEXT,
    base_award_value      REAL,
    base_closing_date     TEXT,
    base_doc_versions     INTEGER,         -- sum(version_count) over its documents
    last_notified_at      TEXT,
    notified_count        INTEGER NOT NULL DEFAULT 0,
    active                INTEGER NOT NULL DEFAULT 1,
    UNIQUE(subscription_id, tender_id)
);
CREATE INDEX IF NOT EXISTS idx_alerts_sub ON tender_alerts(subscription_id);
CREATE INDEX IF NOT EXISTS idx_alerts_tender ON tender_alerts(tender_id);

-- FTS5 over tender metadata (external content -> tenders).
-- `porter` wraps unicode61 with the Porter stemmer, so a search for "bollard"
-- also finds "bollards" and "drilling" finds "drilled". Without it FTS5 matches
-- whole tokens only, and the archive answered those as two unrelated searches —
-- 22 tenders for one, 17 for the other, with no hint that either was partial.
-- Stemming is applied to the query and the index alike, so both spellings
-- reduce to the same term and the two searches converge.
-- It is English-only by construction; Tamil place names and reference codes
-- pass through unchanged, which is what we want.
CREATE VIRTUAL TABLE IF NOT EXISTS tenders_fts USING fts5(
    tender_id UNINDEXED,
    title,
    work_description,
    organisation_chain,
    location,
    reference_number,
    tokenize = 'porter unicode61 remove_diacritics 2'
);

-- FTS5 over extracted document text.
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    document_id UNINDEXED,
    tender_id UNINDEXED,
    filename,
    text,
    tokenize = 'porter unicode61 remove_diacritics 2'
);
"""


# The continuous scraper holds write locks for seconds at a time. Without a
# busy_timeout every other writer fails instantly with "database is locked", and
# callers that swallow that error (explode_zip did) silently skip irreplaceable
# work. Waiting is always better than losing a document.
BUSY_TIMEOUT_MS = 30_000


# Read-side tuning for the web process. The archive is far larger than any
# working set a page needs, so the wins come from not re-reading the same pages:
# a 64 MB page cache holds the hot b-tree interiors and the FTS term index, and
# mmap lets SQLite read pages straight out of the page cache of the OS instead of
# copying them through a syscall. temp_store=MEMORY keeps the sorter that
# ORDER BY/GROUP BY falls back to off the disk.
_READ_PRAGMAS = (
    "PRAGMA cache_size = -65536",
    "PRAGMA mmap_size = 1073741824",
    "PRAGMA temp_store = MEMORY",
)


def connect(db_path: Path, *, read_only: bool = False,
            busy_timeout_ms: int = BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a connection. Read-only mode is used by the web app."""
    db_path = Path(db_path)
    if read_only:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    if read_only:
        for pragma in _READ_PRAGMAS:
            conn.execute(pragma)
    return conn


class ThreadLocalReader:
    """One read-only connection per thread, opened on first use.

    Sharing a single ``sqlite3.Connection`` across threads is not merely slow, it
    returns *wrong data*: CPython's sqlite3 caches prepared statements per
    connection and keys them by SQL text, so two threads running the same query
    — which is exactly what two people searching at once do — drive one
    ``sqlite3_stmt`` from both sides. Measured on this archive with 8 threads,
    ``count(*)`` came back as 0 or as no row at all in ~10% of calls and a
    ``LIMIT 25`` page returned anywhere from 0 to 68 rows. For an evidence
    archive that is the worst possible failure: results silently go missing.

    Connections are never closed. The pool is bounded by the server's thread
    pool, and a read-only WAL reader that holds no transaction costs nothing to
    keep open.
    """

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._local = threading.local()

    def __call__(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self._db_path, read_only=True)
            self._local.conn = conn
        return conn


# Columns added after the initial release. ALTER TABLE ADD COLUMN is the only
# schema change SQLite applies in place, and it is a no-op-safe operation as
# long as we check first — so migration is just "add what is missing".
_MIGRATIONS: dict[str, dict[str, str]] = {
    "tenders": {
        "short_name": "TEXT",
        # Set from a 'Cancellation of Tender' corrigendum on the detail page.
        # Sticky once set: the portal can drop a corrigendum row, but a tender
        # that was cancelled stays cancelled in the archive.
        "cancelled_at": "TEXT",
        "cancellation_note": "TEXT",
        "corrigendum_count": "INTEGER NOT NULL DEFAULT 0",
        # Kept out of raw_json: that column is a flat label->value map the web
        # app renders verbatim, and a nested list would break it.
        "corrigenda_json": "TEXT",
        # Award of Contract. None of this comes from the detail page — that page
        # is silent about who won, whatever the tender's stage (verified). It is
        # recovered from the AOC document the portal publishes under
        # ResultOfTenders, so every column here is populated by enrich_awards.
        #
        # award_value_num is deliberately a *separate* column from
        # tender_value_num rather than a correction to it. The estimate and the
        # accepted bid are two different facts, and the distance between them is
        # frequently the only visible sign that something went wrong; merging
        # them would erase the finding.
        "award_stage": "TEXT",          # 'AOC' — the portal's own stage label
        "awarded_at": "TEXT",           # ISO date; the portal's "AOC Date"
        "awarded_to": "TEXT",           # successful bidder (L1), when parseable
        "award_value_num": "REAL",
        "award_value_raw": "TEXT",
        "award_ref": "TEXT",            # the AOC document's published filename
        "award_signatory": "TEXT",      # officer who digitally signed the award
        "award_bidders_json": "TEXT",   # full comparative statement, nested
        # Set on every probe, hit or miss, so a sweep is resumable and a tender
        # with no award document is not re-asked for one every run.
        "award_probed_at": "TEXT",
        # Progressive retry bookkeeping for capture_retry.py — the frequent,
        # lightweight sweep that gets a newly-discovered open tender's detail
        # page (and hence its documents) read on the 1m/5m/15m/30m/60m schedule
        # in doc_lifecycle.next_attempt_after, independent of the much slower
        # forward_capture cycle. Cleared back to 0/NULL once the tender is
        # 'detailed' with nothing left pending/failed/lost — "done, stop
        # asking" — so a completed tender costs nothing to re-check for.
        "capture_attempts": "INTEGER NOT NULL DEFAULT 0",
        "next_capture_at": "TEXT",
    },
    "documents": {
        # Bounded retry bookkeeping for documents we have not captured yet.
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "last_attempt_at": "TEXT",
        "next_attempt_at": "TEXT",
        # Instrumentation for the publish-lag question (see document_events).
        "first_seen_at": "TEXT",
        "first_seen_missing_at": "TEXT",
        "link_first_seen_at": "TEXT",
        # Integrity/modification bookkeeping.
        "verified_at": "TEXT",
        "recheck_at": "TEXT",
        "version_count": "INTEGER NOT NULL DEFAULT 1",
    },
}


# Every column the web app's Advanced panel can filter on. Without these each
# filter is a full table scan, and the two raw_json ones also JSON-parse every
# tender: measured against a 90,520-tender copy, a form-of-contract filter went
# 386 ms -> 1.3 ms and a value range 106 ms -> 0.5 ms. They cost ~10 MB at that
# size and 16 extra b-tree writes per tender, which the scraper — rate-limited
# to a handful of tenders a minute by its own politeness delay — will not feel.
#
# The Selection Criteria flags get *partial* indexes instead of plain ones. They
# are yes/no fields that are overwhelmingly "No", so indexing only the 'Yes'
# rows is what the query asks for and keeps six of the eight under 10 KB.
#
# A partial index is only usable when its predicate matches the WHERE term
# character for character, so this tuple has to stay in step with the values of
# ``web.search.CRITERIA`` — a silent divergence would not break a search, it
# would just quietly stop indexing it. test_search.py asserts they agree.
CRITERIA_FLAGS = (
    "Allow Two Stage Bidding",
    "Should Allow NDA Tender",
    "Allow Preferential Bidder",
    "General Technical Evaluation Allowed",
    "ItemWise Technical Evaluation Allowed",
    "Tender Fee Exemption Allowed",
    "EMD Exemption Allowed",
    "Withdrawal Allowed",
)

_SEARCH_INDEXES = """
    -- Serves the default browse order (closing_date DESC, tender_id DESC) as a
    -- plain reverse index walk, tiebreaker included. idx_tenders_closing cannot:
    -- it stops at closing_date, so the tie still needs a sort.
    CREATE INDEX IF NOT EXISTS idx_tenders_closing_id
        ON tenders(closing_date, tender_id);
    CREATE INDEX IF NOT EXISTS idx_tenders_published
        ON tenders(substr(published_date, 1, 10));
    CREATE INDEX IF NOT EXISTS idx_tenders_value ON tenders(tender_value_num);
    CREATE INDEX IF NOT EXISTS idx_tenders_category ON tenders(tender_category);
    CREATE INDEX IF NOT EXISTS idx_tenders_type ON tenders(tender_type);
    CREATE INDEX IF NOT EXISTS idx_tenders_pcategory ON tenders(product_category);
    CREATE INDEX IF NOT EXISTS idx_tenders_pincode ON tenders(pincode);
    CREATE INDEX IF NOT EXISTS idx_tenders_form_of_contract
        ON tenders(json_extract(raw_json, '$."Form Of Contract"'));
    CREATE INDEX IF NOT EXISTS idx_tenders_payment_mode
        ON tenders(json_extract(raw_json, '$."Payment Mode"'));
""" + "".join(
    f"""
    CREATE INDEX IF NOT EXISTS idx_tenders_crit_{i} ON tenders(tender_id)
        WHERE json_extract(raw_json, '$."{flag}"') = 'Yes';"""
    for i, flag in enumerate(CRITERIA_FLAGS)
)


# Table names SCHEMA is responsible for, read out of SCHEMA itself so a new
# CREATE TABLE cannot be added without the pre-migration check learning about it.
_SCHEMA_TABLES = tuple(re.findall(
    r"CREATE (?:VIRTUAL )?TABLE IF NOT EXISTS (\w+)", SCHEMA))


def _pending_migration(conn: sqlite3.Connection) -> list[str]:
    """What ``init_db`` would have to change, named. Empty means nothing to do.

    Every check is a catalogue read, so calling this on each start-up of the web
    app costs nothing — which is what lets the backup below be conditional
    rather than a 500 MB copy on every process launch.
    """
    have_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    pending = [f"table {t}" for t in _SCHEMA_TABLES if t not in have_tables]
    for table, columns in _MIGRATIONS.items():
        if table not in have_tables:
            continue
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        pending += [f"{table}.{name}" for name in columns if name not in have]
    return pending


def backup_db(db_path: Path, tag: str) -> Path | None:
    """Snapshot the live database into ``data/backups/`` and return the path.

    ``Connection.backup`` is the only correct way to copy a database that is
    being written to: it takes a read transaction for the copy, so the result is
    a consistent point-in-time image, whereas ``cp`` of a WAL database races the
    scraper and yields a file whose WAL and main file disagree. Single-step
    (``pages=-1``) on purpose — a paged copy would be restarted by every
    concurrent commit, and this archive is committed to continuously.
    """
    dest_dir = Path(db_path).parent / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest = dest_dir / f"{Path(db_path).stem}-{stamp}-pre-{tag}.db"
    src = connect(db_path, read_only=True)
    try:
        out = sqlite3.connect(dest)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dest


def init_db(db_path: Path, *, backup: bool = True) -> None:
    """Create tables and FTS indexes if they do not exist, then migrate.

    A schema change to a populated archive is snapshotted first. The snapshot is
    skipped when there is nothing pending (the overwhelmingly common case: every
    process start-up) and when the database has no ``tenders`` table yet, which
    is a fresh create with nothing to lose.
    """
    conn = connect(db_path)
    try:
        pending = _pending_migration(conn)
        populated = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tenders'"
        ).fetchone() is not None
        if pending and populated and backup:
            conn.close()
            dest = backup_db(db_path, "schema")
            log.info("schema change pending (%s); snapshot at %s",
                     ", ".join(pending), dest)
            conn = connect(db_path)
        conn.executescript(SCHEMA)
        for table, columns in _MIGRATIONS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        # Indexes over migrated columns must come after the ALTERs.
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_docs_next_attempt
                ON documents(next_attempt_at) WHERE status <> 'captured';
            CREATE INDEX IF NOT EXISTS idx_docs_verified
                ON documents(verified_at) WHERE status = 'captured';
            CREATE INDEX IF NOT EXISTS idx_tenders_cancelled
                ON tenders(cancelled_at);
            CREATE INDEX IF NOT EXISTS idx_tenders_awarded
                ON tenders(awarded_at);
            -- Partial: the enricher's work queue is "never probed", and once a
            -- sweep completes that predicate matches almost nothing, so the
            -- index stays tiny while the queue scan stays an index seek.
            CREATE INDEX IF NOT EXISTS idx_tenders_award_unprobed
                ON tenders(tender_id) WHERE award_probed_at IS NULL;
            -- capture_retry.py's due-check; partial because a completed or
            -- never-scheduled tender (the overwhelming majority) has NULL here.
            CREATE INDEX IF NOT EXISTS idx_tenders_next_capture
                ON tenders(next_capture_at) WHERE next_capture_at IS NOT NULL;
        """)
        conn.executescript(_SEARCH_INDEXES)
        conn.commit()
    finally:
        conn.close()


def document_versions(conn: sqlite3.Connection, document_id: int) -> list[dict]:
    """The full version chain for a document, newest first.

    The live ``documents`` row is the head of the chain and is returned as the
    first element with ``superseded_at = None``; older entries come from
    ``document_versions`` and keep their own preserved ``stored_path``.
    """
    head = conn.execute(
        "SELECT id AS document_id, tender_id, filename, sha256, byte_size,"
        " stored_path, content_type, downloaded_at AS captured_at,"
        " NULL AS superseded_at, version_count"
        " FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    if head is None:
        return []
    chain = [dict(head) | {"current": True}]
    for row in conn.execute(
        "SELECT document_id, tender_id, filename, sha256, byte_size, stored_path,"
        " content_type, captured_at, superseded_at FROM document_versions"
        " WHERE document_id = ? ORDER BY superseded_at DESC, id DESC",
        (document_id,),
    ):
        chain.append(dict(row) | {"current": False})
    return chain


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
