"""Validity + completeness audit of captured documents.

``status='captured'`` is a claim, not a fact: the file can have been truncated
by a killed process, silently replaced by an HTML error page the downloader
accepted, or lost when a disk was restored from a partial backup. This module
re-derives the claim from the bytes on disk:

* the file exists at ``stored_path`` and is non-empty,
* its length matches ``byte_size`` and its SHA-256 matches ``sha256``,
* it is not an HTML page saved under a .pdf/.xls name
  (``download_docs._looks_like_html``, the same test the downloader uses),
* it opens structurally for its type — a PDF with a header but no ``%%EOF``, or
  a zip whose central directory will not read, is truncated garbage.

Anything that fails is put back into the capture queue (``pending`` when the
portal still offers a link, otherwise ``lost``) so the ordinary retry loop
re-downloads it. **Everything that passes is left completely alone** — a valid
file is never re-fetched. Nothing under ``data/docs`` is ever deleted: a file
that failed the audit is copied into the document's ``versions/`` directory and
recorded, so even a corrupt capture stays inspectable.

Hashing thousands of files is I/O-heavy, so the scheduled pass is bounded
(``limit``) and skips anything verified within ``max_age_hours``. A full sweep
therefore happens continuously in small slices rather than as one stall.
"""

from __future__ import annotations

import hashlib
import logging
import os
import zipfile
from pathlib import Path

from .config import load_config
from .db import connect, init_db
from .doc_lifecycle import record_event
from .download_docs import _looks_like_html, _preserve_previous
from .util import now_iso

log = logging.getLogger("integrity")

DEFAULT_BATCH = 500
DEFAULT_MAX_AGE_H = 12

# Bytes read from the tail when looking for a format's end-of-file marker.
_TAIL = 4096


def _hash_and_head(path: Path) -> tuple[str, int, bytes, bytes]:
    """One pass over the file: sha256, size, first 512 bytes, last 4 KiB."""
    sha = hashlib.sha256()
    size = 0
    head = b""
    tail = b""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            if not head:
                head = chunk[:512]
            sha.update(chunk)
            size += len(chunk)
            tail = (tail + chunk)[-_TAIL:]
    return sha.hexdigest(), size, head, tail


def _structural_ok(path: Path, head: bytes, tail: bytes, *, deep: bool) -> str | None:
    """None if the file opens for its type, else a short failure reason."""
    ext = path.suffix.lower()
    if ext == ".pdf" or head[:4] == b"%PDF":
        if head[:4] != b"%PDF":
            return "pdf_no_header"
        # Truncation is the failure mode that matters; a PDF always ends with
        # %%EOF (possibly followed by whitespace or a stray byte or two).
        if b"%%EOF" not in tail:
            return "pdf_truncated"
        if deep:
            try:
                import pdfplumber

                with pdfplumber.open(str(path)) as pdf:
                    if not pdf.pages:
                        return "pdf_no_pages"
            except Exception as exc:  # noqa: BLE001
                return f"pdf_unreadable:{type(exc).__name__}"
        return None

    # .xlsx/.docx and real zips are all zip containers.
    if ext in (".zip", ".xlsx", ".xlsm", ".docx", ".pptx") or head[:2] == b"PK":
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                if not names:
                    return "zip_empty"
                if deep and zf.testzip() is not None:
                    return "zip_crc_error"
        except Exception as exc:  # noqa: BLE001
            return f"zip_unreadable:{type(exc).__name__}"
        return None

    if ext == ".xls":
        # Legacy BIFF is an OLE2 compound file; GePNIC also serves .xls names
        # holding real OOXML (caught by the PK branch above) or HTML tables
        # (caught by the HTML test before this runs).
        if head[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            return "xls_not_ole2"
        return None

    return None


def verify_document(row, root: Path, *, deep: bool = False) -> tuple[bool, str | None,
                                                                    str | None, int]:
    """Audit one captured document. Returns (ok, reason, actual_sha, actual_size)."""
    rel = row["stored_path"]
    if not rel:
        return False, "no_stored_path", None, 0
    path = root / rel
    if not path.exists():
        return False, "missing_file", None, 0
    try:
        sha, size, head, tail = _hash_and_head(path)
    except OSError as exc:
        return False, f"unreadable:{exc.errno}", None, 0
    if size == 0:
        return False, "zero_bytes", sha, 0
    if row["byte_size"] is not None and int(row["byte_size"]) != size:
        return False, f"size_mismatch:{row['byte_size']}!={size}", sha, size
    if row["sha256"] and row["sha256"] != sha:
        return False, "sha_mismatch", sha, size
    if _looks_like_html(head, row["content_type"] or ""):
        # An HTML error page saved as Tendernotice_1.pdf is the single most
        # dangerous silent failure: it looks captured and reads as nothing.
        if path.suffix.lower() not in (".html", ".htm"):
            return False, "html_error_page", sha, size
    reason = _structural_ok(path, head, tail, deep=deep)
    if reason:
        return False, reason, sha, size
    return True, None, sha, size


def run_verify(db_path=None, *, limit: int = DEFAULT_BATCH,
               max_age_hours: int = DEFAULT_MAX_AGE_H, deep: bool = False,
               tender_id: str | None = None, repair: bool = True,
               progress=None) -> dict:
    """Verify a bounded slice of captured documents, oldest-checked first.

    ``limit=0`` audits everything due. ``repair=False`` reports without touching
    any row, which is what you want before trusting a first full sweep.
    """
    _p = progress or (lambda msg: log.info(msg))
    cfg = load_config()
    db_path = db_path or cfg.db_path
    init_db(db_path)
    conn = connect(db_path)
    root = cfg.db_path.parent.parent

    where = ["status = 'captured'"]
    params: list = []
    if tender_id:
        where.append("tender_id = ?")
        params.append(tender_id)
    elif max_age_hours:
        where.append("(verified_at IS NULL OR datetime(verified_at) <="
                     f" datetime('now', '-{int(max_age_hours)} hours'))")
    sql = ("SELECT id, tender_id, filename, stored_path, byte_size, sha256,"
           " content_type, download_url, downloaded_at, status FROM documents"
           " WHERE " + " AND ".join(where) +
           " ORDER BY (verified_at IS NOT NULL), verified_at")
    if limit:
        sql += f" LIMIT {int(limit)}"

    rows = conn.execute(sql, params).fetchall()
    result = {"checked": 0, "valid": 0, "invalid": 0, "requeued": 0,
              "reasons": {}, "examples": []}
    try:
        for row in rows:
            ok, reason, _sha, _size = verify_document(row, root, deep=deep)
            result["checked"] += 1
            if ok:
                result["valid"] += 1
                if repair:
                    conn.execute("UPDATE documents SET verified_at=? WHERE id=?",
                                 (now_iso(), row["id"]))
                continue
            result["invalid"] += 1
            result["reasons"][reason] = result["reasons"].get(reason, 0) + 1
            if len(result["examples"]) < 20:
                result["examples"].append(
                    {"id": row["id"], "tender_id": row["tender_id"],
                     "filename": row["filename"], "reason": reason})
            _p(f"  ✗ {row['tender_id']}/{row['filename']}: {reason}")
            if not repair:
                continue
            _requeue(conn, cfg, row, reason)
            result["requeued"] += 1
            if result["requeued"] % 50 == 0:
                conn.commit()
        conn.commit()
    finally:
        conn.close()
    return result


def _requeue(conn, cfg, row, reason: str) -> None:
    """Put a failed document back in the capture queue without losing anything.

    The bad bytes are preserved as a superseded version before the row's hash is
    cleared, so re-downloading records a clean capture rather than a spurious
    "modification", and the corrupt copy stays on disk for inspection.
    """
    try:
        _preserve_previous(conn, cfg, row, row["id"])
    except Exception as exc:  # noqa: BLE001
        log.warning("could not preserve failed capture %s: %s", row["stored_path"], exc)
    record_event(conn, row["id"], row["tender_id"], "integrity_failed", reason)
    conn.execute(
        "UPDATE documents SET status=?, sha256=NULL, byte_size=NULL,"
        " verified_at=?, attempts=0, next_attempt_at=NULL, recheck_at=NULL"
        " WHERE id=?",
        ("pending" if row["download_url"] else "lost", now_iso(), row["id"]),
    )
    # The extracted text described the bad file; drop it so a good capture
    # re-extracts rather than leaving the index describing garbage.
    conn.execute("DELETE FROM doc_text WHERE document_id=?", (row["id"],))


def be_nice() -> None:
    """Lower priority for large manual sweeps: the live scraper matters more."""
    try:
        os.nice(10)
    except OSError:  # pragma: no cover - not permitted in some sandboxes
        pass
