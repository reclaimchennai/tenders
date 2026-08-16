"""(Re)build the FTS5 search indexes from the base tables.

Kept explicit (rather than trigger-driven) so bulk loads stay fast and the sync
point is obvious. Safe to run repeatedly.

Both indexes are synced **incrementally**. ``docs_fts`` always was: it covers
the full text of every captured document, and rewriting that on every scraper
cycle would hold SQLite's single write lock for minutes and lock out the live
capture pass.

``tenders_fts`` used to be rebuilt wholesale on every call, on the reasoning
that it was "a few thousand short rows" edited in place, so nothing cheaper
stayed correct. That reasoning expired: the archive reached 95,779 tenders, and
``scripts/extract_loop.py`` calls this function after every extraction slice.
With a steady trickle of 1-3 newly captured documents the slices returned in
~8s, so a full ``DELETE`` + re-``INSERT`` of all 95,779 rows ran roughly every
14 seconds, continuously. That single mistake took the public site down on
2026-08-15: it saturated disk I/O, held the write lock almost permanently (the
web app's own writes started failing with "database is locked"), and grew the
WAL to **44 GB against a 1.9 GB database** — SQLite reuses but never shrinks a
WAL, so the space stayed gone until a manual TRUNCATE checkpoint. The web
process stopped answering entirely while still looking healthy to systemd.

So the tender side is now watermarked on ``tenders.last_updated_at`` and
touches only rows that actually changed. A steady state where nothing changed
writes nothing at all, which is the property that matters: this function is
called on a loop, and it must be free when there is no work.

``docs_fts`` is an **external-content** FTS5 table reading through the
``docs_fts_source`` view. As a default (contentful) table it stored a second
verbatim copy of every extracted document — measured at 97.8 MB of
``docs_fts_content`` against 97.8 MB of ``doc_text``, and growing one-for-one
with the archive. External content keeps only the inverted index and fetches
text from ``doc_text`` when a snippet is needed. The cost is that FTS5 no longer
notices writes to ``doc_text``: every change has to be pushed in here (see
``index_document``), and a mismatch is repaired by ``rebuild_fts(full=True)``.

The incremental path relies on ``docs_fts.rowid == documents.id``, which makes
"which documents are missing from the index?" a cheap rowid lookup instead of a
full scan of the index content. Databases predating either invariant are
detected via ``fts_state`` and repaired by one full rebuild.

Column order is load-bearing: the web layer calls ``snippet(docs_fts, 3, ...)``,
so ``text`` must stay the fourth column.
"""

from __future__ import annotations

import logging

from .config import load_config
from .db import connect, init_db

log = logging.getLogger("index_fts")

# db.connect() sets no busy_timeout and the live scraper holds write locks;
# without this an index refresh dies on "database is locked".
BUSY_TIMEOUT_MS = 30_000

_ALIGNED_KEY = "docs_fts_rowid_aligned"
_EXTERNAL_KEY = "docs_fts_external_content"
# Max tenders.last_updated_at that tenders_fts is known to cover.
_TENDERS_WATERMARK_KEY = "tenders_fts_synced_through"

_SOURCE_VIEW = "docs_fts_source"

# FTS5 external content matches the content table's columns by name, and
# doc_text has neither tender_id nor filename — hence a view rather than
# content='doc_text'. Restricting it to non-empty text is what keeps
# unextractable documents (zip bundles, RARs, failures) out of the index.
_SOURCE_VIEW_SQL = f"""
CREATE VIEW {_SOURCE_VIEW} AS
    SELECT t.document_id AS document_id,
           d.tender_id   AS tender_id,
           d.filename    AS filename,
           t.text        AS text
    FROM doc_text t
    JOIN documents d ON d.id = t.document_id
    WHERE t.text IS NOT NULL AND t.text != ''
"""

_DOCS_FTS_SQL = f"""
CREATE VIRTUAL TABLE docs_fts USING fts5(
    document_id UNINDEXED,
    tender_id UNINDEXED,
    filename,
    text,
    content='{_SOURCE_VIEW}',
    content_rowid='document_id',
    tokenize = 'unicode61 remove_diacritics 2'
)
"""

_FTS_COLUMNS = "rowid, document_id, tender_id, filename, text"
_SOURCE_SELECT = (
    f"SELECT document_id, document_id, tender_id, filename, text FROM {_SOURCE_VIEW}"
)


def open_writer(db_path):
    """Connection set up for write batches that share the DB with the scraper.

    Autocommit plus explicit BEGIN IMMEDIATE, because the default deferred
    transaction takes a read snapshot first and SQLite then refuses the upgrade
    to a write outright — busy_timeout does not apply to that case, so a passing
    scraper commit kills the batch after milliseconds. Grabbing the write lock
    up front turns the same contention into an ordinary, survivable wait.
    """
    conn = connect(db_path)
    conn.isolation_level = None
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def begin_immediate(conn, *, attempts: int = 5) -> None:
    """Open a write transaction, retrying briefly and then failing loudly."""
    import time

    if conn.in_transaction:
        return
    for attempt in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            log.warning("write lock unavailable (attempt %d/%d): %s",
                        attempt + 1, attempts, exc)
            time.sleep(2 * (attempt + 1))
    raise sqlite3.OperationalError("could not acquire the write lock")


def commit(conn, *, attempts: int = 5) -> None:
    """Commit, retrying on lock contention and raising loudly if it never
    succeeds — a swallowed lock error means silently discarded work."""
    import time

    for attempt in range(attempts):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            log.warning("commit blocked by lock (attempt %d/%d): %s",
                        attempt + 1, attempts, exc)
            time.sleep(2 * (attempt + 1))
    raise sqlite3.OperationalError("could not commit: database stayed locked")


def _ensure_state_table(conn) -> None:
    begin_immediate(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS fts_state (key TEXT PRIMARY KEY, value TEXT)")
    commit(conn)


def _flag(conn, key: str) -> bool:
    row = conn.execute("SELECT value FROM fts_state WHERE key=?", (key,)).fetchone()
    return bool(row and row[0] == "1")


def _set_flag(conn, key: str) -> None:
    conn.execute("INSERT OR REPLACE INTO fts_state (key, value) VALUES (?, '1')", (key,))


def _state_get(conn, key: str) -> str | None:
    """Read an arbitrary fts_state value (``_flag`` only answers the '1' case)."""
    row = conn.execute("SELECT value FROM fts_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _state_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO fts_state (key, value) VALUES (?, ?)",
                 (key, value))


_TENDER_FTS_COLUMNS = ("tender_id, title, work_description, organisation_chain,"
                       " location, reference_number")


def _rebuild_tenders_full(conn) -> int:
    conn.execute("DELETE FROM tenders_fts")
    conn.execute(f"INSERT INTO tenders_fts ({_TENDER_FTS_COLUMNS})"
                 f" SELECT {_TENDER_FTS_COLUMNS} FROM tenders")
    return conn.execute("SELECT count(*) FROM tenders_fts").fetchone()[0]


def _sync_tenders_incremental(conn, *, full: bool = False) -> int:
    """Index only the tenders that changed since the last sync.

    Watermarked on ``tenders.last_updated_at``, which every write path already
    maintains (pipeline._store, latest_active.record_row, the enumerators).

    Two conditions fall back to a full rebuild rather than trusting the
    watermark, because a silently under-indexed archive is the one failure a
    search mirror must never have:

    * no watermark stored — a database from before this change, or a fresh one;
    * the index and the table disagree on row count, which means something was
      inserted by a path that did not come through here.

    The comparison is ``>=`` rather than ``>`` on purpose. The watermark is the
    max ``last_updated_at`` observed at sync time, and a writer committing in
    that same second would otherwise be skipped forever. Re-syncing the
    boundary row on the next pass costs one delete+insert and is idempotent;
    missing it costs a tender that can never be found again.

    Both sides of that comparison go through SQLite's ``datetime()`` rather than
    comparing the stored strings directly. Every writer here happens to use
    ``util.now_iso`` today (all 95,798 rows are ``...THH:MM:SS+00:00``), but a
    plain string compare silently depends on that: a row written as
    ``'2026-08-15 17:00:00'`` sorts *before* a watermark of
    ``'2026-08-15T16:59:00'``, because ``' ' < 'T'`` — so it would be skipped
    forever and that tender would never appear in search again. A test caught
    exactly this. ``datetime()`` normalises both formats to a common form, so
    the invariant no longer rests on every future writer picking the same
    string format. NULLs are swept in for the same reason: they can never
    satisfy a ``>=`` and would otherwise be indexed once and never refreshed.
    """
    counted = conn.execute("SELECT count(*) FROM tenders").fetchone()[0]
    indexed = conn.execute("SELECT count(*) FROM tenders_fts").fetchone()[0]
    watermark = _state_get(conn, _TENDERS_WATERMARK_KEY)

    if full or watermark is None or counted != indexed:
        if not full and watermark is not None and counted != indexed:
            log.info("tenders_fts has %d rows against %d tenders; rebuilding",
                     indexed, counted)
        n = _rebuild_tenders_full(conn)
    else:
        changed = [r[0] for r in conn.execute(
            "SELECT tender_id FROM tenders"
            " WHERE last_updated_at IS NULL"
            "    OR datetime(last_updated_at) >= datetime(?)", (watermark,))]
        if changed:
            # executemany over the id list rather than a correlated subquery:
            # the set is small (what changed since the last pass, seconds ago)
            # and this keeps the write lock held for exactly that many rows.
            conn.executemany("DELETE FROM tenders_fts WHERE tender_id = ?",
                             [(t,) for t in changed])
            conn.executemany(
                f"INSERT INTO tenders_fts ({_TENDER_FTS_COLUMNS})"
                f" SELECT {_TENDER_FTS_COLUMNS} FROM tenders WHERE tender_id = ?",
                [(t,) for t in changed])
        n = indexed if not changed else \
            conn.execute("SELECT count(*) FROM tenders_fts").fetchone()[0]

    # Stored already normalised, so the watermark itself is never the thing
    # that reintroduces a format mismatch.
    high = conn.execute("SELECT max(datetime(last_updated_at)) FROM tenders").fetchone()[0]
    if high:
        _state_set(conn, _TENDERS_WATERMARK_KEY, high)
    return n


def _ensure_external_content(conn) -> None:
    """Convert a legacy contentful docs_fts to external content, once.

    db.py still declares the contentful form with CREATE ... IF NOT EXISTS, so a
    freshly created database arrives here needing the same conversion; keying
    off the stored DDL rather than a flag makes that self-healing.
    """
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='docs_fts'"
    ).fetchone()
    if ddl and f"content='{_SOURCE_VIEW}'" in (ddl[0] or ""):
        return
    log.info("converting docs_fts to external content (drops the duplicate text copy)")
    begin_immediate(conn)
    conn.execute("DROP TABLE IF EXISTS docs_fts")
    conn.execute(f"DROP VIEW IF EXISTS {_SOURCE_VIEW}")
    conn.execute(_SOURCE_VIEW_SQL)
    conn.execute(_DOCS_FTS_SQL)
    conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
    _set_flag(conn, _ALIGNED_KEY)
    _set_flag(conn, _EXTERNAL_KEY)
    commit(conn)


def _indexed_count(conn) -> int:
    """Rows in the index, counted from the docsize shadow table.

    ``count(*)`` on an external-content table joins back to the content view for
    every row; the shadow table answers the same question from an integer index.
    """
    try:
        return conn.execute("SELECT count(*) FROM docs_fts_docsize").fetchone()[0]
    except Exception:  # noqa: BLE001 - shadow table layout is not contractual
        return conn.execute("SELECT count(*) FROM docs_fts").fetchone()[0]


def _rebuild_docs_full(conn) -> int:
    conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
    _set_flag(conn, _ALIGNED_KEY)
    return _indexed_count(conn)


def _snapshot_rowids(conn) -> None:
    """Copy the indexed document ids into temp._fts_have.

    Reads fts5's ``%_docsize`` shadow table, whose primary key is the rowid, so
    this is an index-only scan. The obvious ``SELECT rowid FROM docs_fts`` is a
    full scan that decodes the stored text of every row — tolerable at 5k
    documents, ruinous as the archive grows.
    """
    conn.execute("DROP TABLE IF EXISTS temp._fts_have")
    conn.execute("CREATE TEMP TABLE _fts_have (doc_id INTEGER PRIMARY KEY)")
    try:
        conn.execute("INSERT INTO temp._fts_have SELECT id FROM docs_fts_docsize")
    except Exception:  # noqa: BLE001 - shadow table layout is not contractual
        conn.execute("INSERT INTO temp._fts_have SELECT rowid FROM docs_fts")


def _sync_docs_incremental(conn) -> int:
    """Add newly extracted documents; return the resulting row count.

    Retiring an entry needs the text that was indexed, and for a document that
    has already lost its doc_text row that text is simply gone — so stale
    entries are repaired by a full rebuild instead. In normal operation there
    are none: index_document removes an entry at the moment its text changes.
    """
    _snapshot_rowids(conn)

    stale = conn.execute(
        f"SELECT count(*) FROM temp._fts_have "
        f"WHERE doc_id NOT IN (SELECT document_id FROM {_SOURCE_VIEW})"
    ).fetchone()[0]
    if stale:
        log.info("docs_fts has %d stale entr(ies); rebuilding", stale)
        conn.execute("DROP TABLE temp._fts_have")
        return _rebuild_docs_full(conn)

    conn.execute(
        f"INSERT INTO docs_fts ({_FTS_COLUMNS}) {_SOURCE_SELECT} "
        "WHERE document_id NOT IN (SELECT doc_id FROM temp._fts_have)"
    )
    conn.execute("DROP TABLE temp._fts_have")
    return _indexed_count(conn)


def ensure_docs_fts_aligned(conn) -> None:
    """Bring docs_fts to the shape index_document() requires, repairing once.

    Callers that write index rows themselves (extract_text) need external
    content in place and ``rowid == documents.id``; inserting at
    rowid=document_id against a legacy index would collide with an unrelated row.
    """
    _ensure_state_table(conn)
    _ensure_external_content(conn)
    if _flag(conn, _ALIGNED_KEY):
        return
    log.info("docs_fts rowids not aligned; rebuilding once")
    _rebuild_docs_full(conn)
    conn.commit()


def index_document(conn, doc_id: int, tender_id, filename, text,
                   old_text: str | None = None) -> None:
    """Refresh one document's entry in docs_fts.

    Called as each document is extracted so the site becomes searchable
    continuously rather than only after a whole catch-up pass finishes — an
    interrupted multi-hour run must still leave everything it did index behind.

    ``old_text`` is the text currently indexed for this document, if any.
    External content means FTS5 cannot look the old terms up itself: they have
    to be handed back verbatim for it to unpick them, and this is the only
    moment they are still available. Callers must pass it whenever they are
    replacing text (re-extraction, a re-published document); omitting it for a
    document that was indexed leaves stale terms that only a full rebuild clears.
    Requires ensure_docs_fts_aligned() to have run on this database.
    """
    if old_text:
        conn.execute(
            f"INSERT INTO docs_fts (docs_fts, {_FTS_COLUMNS}) "
            "VALUES ('delete', ?, ?, ?, ?, ?)",
            (doc_id, doc_id, tender_id, filename, old_text),
        )
    if text:
        conn.execute(
            f"INSERT INTO docs_fts ({_FTS_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            (doc_id, doc_id, tender_id, filename, text),
        )


def rebuild_fts(db_path=None, *, full: bool = False) -> dict:
    cfg = load_config()
    db_path = db_path or cfg.db_path
    init_db(db_path)
    conn = open_writer(db_path)
    try:
        _ensure_state_table(conn)
        _ensure_external_content(conn)

        begin_immediate(conn)
        n_meta = _sync_tenders_incremental(conn, full=full)
        # Released before the (much longer) document pass so the live scraper
        # is not blocked on the write lock any longer than necessary.
        commit(conn)

        begin_immediate(conn)
        if full or not _flag(conn, _ALIGNED_KEY):
            n_docs = _rebuild_docs_full(conn)
        else:
            n_docs = _sync_docs_incremental(conn)
        commit(conn)
    finally:
        conn.close()

    result = {"tenders_indexed": n_meta, "docs_indexed": n_docs}
    log.info("FTS rebuilt: %s", result)
    return result
