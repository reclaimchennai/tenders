"""WAL guard and integrity report for the archive database. Never a repair tool.

On 2026-08-15 the write-ahead log of a 1.9 GB database reached **44 GB** and
took the box down with it. That is not a bug in SQLite; it is how WAL mode is
specified to behave, and it surprises people every time:

* Committing appends frames to ``tenders.db-wal``. The file only ever grows.
* A *passive* checkpoint — which SQLite runs automatically once the WAL passes
  ``wal_autocheckpoint`` pages — copies those frames back into the database and
  then **rewinds the WAL to reuse the space in place**. It does not shorten the
  file. So a WAL that spiked once to 44 GB stays a 44 GB file on disk forever,
  even while reporting that everything is checkpointed and healthy.
* A passive checkpoint also *gives up silently* whenever a reader is still using
  an old snapshot. The runaway extract loop rebuilt the whole 170,000-row search
  index every ~14 seconds and held the write lock continuously, so the automatic
  checkpoints did nothing at all for hours while commits kept appending.

``PRAGMA wal_checkpoint(TRUNCATE)`` is the only operation that returns the
space: it checkpoints everything and then truncates the file to zero. That is
the entire job of this script.

**This script never repairs and never deletes.** The archive is the evidence —
tenders that the portal itself deletes at close exist nowhere else — so if
``quick_check`` reports anything other than ``ok``, the correct response is to
stop, shout, and leave every byte exactly where it is for a human to copy off
the box and examine. An automatic ``REINDEX``, ``VACUUM`` or "rebuild the
damaged table" would destroy the only surviving copy of the damage *and* of
whatever is still readable around it. There is no code path here that writes to
the database other than the checkpoint, which moves already-committed frames
into the file they were always destined for.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import time
from pathlib import Path

# 1 GiB against a 1.9 GB database. Chosen as roughly half the database rather
# than an absolute number: a WAL that size means checkpointing has been starved
# for a long while, and it is still small enough that truncating it is a quick
# operation rather than an hour of I/O. Normal steady state on this box is a few
# megabytes — the WAL was 4 MB when this threshold was picked — so this fires
# only when something is genuinely wrong, which is what makes it worth alarming
# on rather than tuning away.
DEFAULT_WAL_MAX_BYTES = 1024 * 1024 * 1024

# Each checkpoint attempt waits at most this long for the write lock. Bounded on
# purpose: this is a maintenance job that must always yield to live capture, and
# a maintenance job that blocks forever on a busy database is indistinguishable
# from the outage it was meant to prevent.
BUSY_TIMEOUT_MS = 10_000
CHECKPOINT_ATTEMPTS = 3
CHECKPOINT_RETRY_S = 15.0

EXIT_OK = 0
EXIT_INTEGRITY_FAILED = 1
EXIT_CHECKPOINT_BUSY = 2
EXIT_UNUSABLE = 3

log = logging.getLogger("db-maint")


def human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}TB"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def report_sizes(db_path: Path) -> dict[str, int]:
    """Sizes of the three files SQLite keeps for a WAL database.

    The ``-shm`` file is included because it is the tell-tale the incident left
    behind: it is the shared index over WAL frames, so it is sized by the WAL's
    *high-water mark*, and unlike the WAL it is not truncated by a checkpoint at
    all — only by the last connection closing. A 90 MB shm beside a 4 MB WAL is
    a fossil of a WAL that was once enormous, and is the cheapest evidence that
    this has happened before.
    """
    return {
        "db": _size(db_path),
        "wal": _size(db_path.with_name(db_path.name + "-wal")),
        "shm": _size(db_path.with_name(db_path.name + "-shm")),
    }


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the database the way the rest of the project does.

    Reuses ``tenders.db.connect`` so this shares one definition of how the
    archive is opened — same foreign-key and row-factory setup — rather than
    growing a second, subtly different one that could drift. The busy timeout is
    deliberately overridden down from the project's 30 s: a capture that waits
    30 s for a lock is protecting irreplaceable work, but a *maintenance* job
    that does the same is just holding the door.

    Read-write is required — a checkpoint is a write to the main database file
    by definition — but nothing here issues DML, DDL or VACUUM.
    """
    from tenders.db import connect

    return connect(db_path, busy_timeout_ms=BUSY_TIMEOUT_MS)


def quick_check(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
    """``PRAGMA quick_check``: the cheap half of ``integrity_check``.

    It verifies every page, index entry and row structure but skips the
    (expensive) reverse check that every index entry has a matching row. On this
    archive that is the right trade for something running daily: it catches the
    corruption that a torn write or a full disk actually produces, without the
    hours the full check would spend competing with live capture.
    """
    rows = conn.execute("PRAGMA quick_check").fetchall()
    lines = [str(r[0]) for r in rows]
    return (lines == ["ok"]), lines


def checkpoint_truncate(conn: sqlite3.Connection, *,
                        attempts: int = CHECKPOINT_ATTEMPTS,
                        retry_s: float = CHECKPOINT_RETRY_S,
                        sleep=time.sleep) -> tuple[bool, tuple[int, int, int]]:
    """Run ``PRAGMA wal_checkpoint(TRUNCATE)``, tolerating a busy database.

    Returns ``(succeeded, (busy, wal_pages, checkpointed_pages))``.

    ``busy=1`` is a completely ordinary answer, not an error: it means a writer
    held the lock or a reader was still on an old snapshot, so SQLite declined
    rather than waiting. Nothing has been changed and nothing is at risk — the
    only cost of giving up is that the file stays large until tomorrow's run. So
    this retries a bounded number of times and then reports honestly, because a
    maintenance job that loops until it wins would be competing with exactly the
    live capture work it exists to protect.
    """
    result = (1, -1, -1)
    for attempt in range(1, attempts + 1):
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.OperationalError as exc:
            # Raised when even the busy_timeout expires before the lock is
            # available. Same meaning as busy=1, different messenger.
            log.warning("checkpoint attempt %d/%d could not get the lock: %s",
                        attempt, attempts, exc)
            if attempt < attempts:
                sleep(retry_s)
            continue
        result = (int(row[0]), int(row[1]), int(row[2]))
        if result[0] == 0:
            return True, result
        log.warning("checkpoint attempt %d/%d returned busy=1 (wal_pages=%s"
                    " checkpointed=%s): a writer or an old reader snapshot is"
                    " still holding the WAL", attempt, attempts, result[1], result[2])
        if attempt < attempts:
            sleep(retry_s)
    return False, result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--wal-max-mb", type=float,
                   default=float(os.environ.get("TENDERS_WAL_MAX_MB",
                                                DEFAULT_WAL_MAX_BYTES / 1024 / 1024)),
                   help="truncate the WAL once it exceeds this many MB")
    p.add_argument("--force-checkpoint", action="store_true",
                   help="checkpoint regardless of the threshold (still never repairs)")
    args = p.parse_args()

    from tenders.config import load_config, setup_logging

    setup_logging(logging.INFO)
    cfg = load_config()
    db_path = cfg.db_path

    if not db_path.exists():
        log.critical("EMERGENCY: no database at %s", db_path)
        return EXIT_UNUSABLE

    before = report_sizes(db_path)
    threshold = int(args.wal_max_mb * 1024 * 1024)
    log.info("db=%s  wal=%s  shm=%s  (%s)",
             human(before["db"]), human(before["wal"]), human(before["shm"]), db_path)
    log.info("wal threshold: %s", human(threshold))

    try:
        conn = _connect(db_path)
    except sqlite3.Error as exc:
        log.critical("EMERGENCY: cannot open %s: %s", db_path, exc)
        return EXIT_UNUSABLE

    try:
        # Integrity first, and unconditionally. Two reasons for this order: a
        # damaged database must be reported even on a day when the WAL is small
        # and nothing else would have run, and — more importantly — a checkpoint
        # writes WAL frames into the main file, which is the last thing anyone
        # wants to do to a database that has just been found corrupt.
        started = time.monotonic()
        healthy, lines = quick_check(conn)
        elapsed = time.monotonic() - started
        if not healthy:
            log.critical(
                "EMERGENCY: PRAGMA quick_check on %s did NOT return ok after"
                " %.1fs — it returned: %s. Doing NOTHING: no checkpoint, no"
                " repair, no vacuum, nothing deleted. This archive is the only"
                " copy of documents the portal itself has deleted. Stop writing"
                " to this box, copy %s and its -wal and -shm sidecars off it,"
                " and diagnose from the copy.",
                db_path, elapsed, "; ".join(lines[:10]), db_path)
            return EXIT_INTEGRITY_FAILED
        log.info("quick_check: ok (%.1fs)", elapsed)

        if before["wal"] <= threshold and not args.force_checkpoint:
            log.info("wal is %s, at or under the %s threshold — nothing to do",
                     human(before["wal"]), human(threshold))
            return EXIT_OK

        log.warning("wal is %s, over the %s threshold — running"
                    " PRAGMA wal_checkpoint(TRUNCATE)",
                    human(before["wal"]), human(threshold))
        ok, (busy, wal_pages, checkpointed) = checkpoint_truncate(conn)
        after = report_sizes(db_path)
        reclaimed = before["wal"] - after["wal"]

        if not ok:
            log.error(
                "checkpoint did not complete after %d attempts (busy=%s"
                " wal_pages=%s checkpointed=%s); wal is still %s. Nothing is"
                " damaged — the space is simply not reclaimed yet, and the next"
                " daily run will try again. If this persists, something is"
                " holding a write transaction open indefinitely, which is what"
                " happened on 2026-08-15.",
                CHECKPOINT_ATTEMPTS, busy, wal_pages, checkpointed,
                human(after["wal"]))
            return EXIT_CHECKPOINT_BUSY

        log.info("checkpoint complete: busy=%s wal_pages=%s checkpointed=%s",
                 busy, wal_pages, checkpointed)
        log.info("reclaimed %s — wal %s -> %s, shm %s -> %s, db %s",
                 human(reclaimed), human(before["wal"]), human(after["wal"]),
                 human(before["shm"]), human(after["shm"]), human(after["db"]))
        return EXIT_OK
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
