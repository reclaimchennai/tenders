"""Capture live tender documents through the GePNIC captcha gate.

Validated download flow:

1. GET a document's ``DirectLink`` -> a ``DocDownCaptcha`` page with an inline
   image captcha (``frmCaptcha`` form).
2. Solve the captcha and POST the form. A wrong answer re-renders the captcha
   ("Invalid Captcha"); retry with a fresh one.
3. The captcha is **session-wide**: once solved, the server streams every
   tender's documents without a further challenge, so we solve exactly one
   captcha per run (tracked on ``client.captcha_verified``).

Document links carry a per-render ``sp=`` token, and tokens minted before the
session was verified are stale, so after unlocking we re-fetch each tender's
detail page to get working links — including the "Download as zip file" bundle
that holds the work-item documents. Files stream to ``data/docs/<tender_id>/``
with the server's own filename, SHA-256 and size recorded; the per-document
status filter skips already-captured files on re-runs.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path

from .captcha import save_verified_label, solve_image
from .doc_lifecycle import (
    due_clause,
    next_recheck_after,
    note_download_failed,
    record_event,
)
from .jsf import extract_form
from .http_client import HttpClient
from .parse_detail import parse_detail
from .util import now_iso

log = logging.getLogger("download")

_SAFE = re.compile(r"[^A-Za-z0-9._\-() ]+")
# RFC 6266 Content-Disposition filename (handles filename= and filename*=UTF-8'').
_CD_FNAME = re.compile(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", re.IGNORECASE)


def safe_filename(name: str) -> str:
    name = name.strip().replace("/", "_").replace("\\", "_")
    return _SAFE.sub("_", name)[:200] or "document"


def _looks_like_html(content: bytes, content_type: str) -> bool:
    if "text/html" in (content_type or "").lower():
        return True
    head = content[:512].lstrip().lower()
    return head.startswith(b"<html") or head.startswith(b"<!doctype html")


def _disposition_name(resp) -> str | None:
    """The server's own filename for the download (e.g. 'work_815982.zip')."""
    m = _CD_FNAME.search(resp.headers.get("Content-Disposition", "") or "")
    if not m:
        return None
    from urllib.parse import unquote

    return unquote(m.group(1).strip()) or None


def _verify_session(client: HttpClient, cfg, doc_download_url: str,
                    progress=None) -> bool:
    """Solve the document-download captcha once, marking the session verified.

    The GePNIC captcha is session-wide: a single solved captcha unlocks every
    tender's documents for the rest of the session, so we only do this once per
    run and short-circuit thereafter via ``client.captcha_verified``.
    """
    _p = progress or (lambda msg: log.info(msg))
    if client.captcha_verified:
        return True
    attempts = int(cfg.scrape.get("captcha_attempts", 6))
    manual = bool(cfg.scrape.get("captcha_manual", False))
    host = cfg.host
    for i in range(1, attempts + 1):
        page = client.get(doc_download_url)
        # Already verified upstream: the gate streams the file instead of HTML.
        if not page.headers.get("Content-Type", "").startswith("text/html"):
            client.captcha_verified = True
            return True
        form = extract_form(page.text, "frmCaptcha")
        if not form or not form.get("captcha_src"):
            # No captcha challenge on an HTML page → session already unlocked.
            client.captcha_verified = True
            return True
        _p(f"    captcha attempt {i}/{attempts}…")
        solution = solve_image(form["captcha_src"], manual=manual)
        if not solution:
            _p(f"    attempt {i}: no solution")
            continue
        _p(f"    attempt {i}: trying {solution!r}…")
        fields = dict(form["fields"])
        fields["captchaText"] = solution
        fields["Submit"] = "Submit"
        resp = client.post(host + form["action"], data=fields)
        if "Invalid Captcha" in resp.text:
            _p(f"    attempt {i}: ✗ wrong ({solution!r})")
            continue
        # Server accepted it — verified solution doubles as a free training label.
        saved = save_verified_label(form["captcha_src"], solution, cfg)
        client.captcha_verified = True
        _p(f"    attempt {i}: ✓ captcha solved, session unlocked"
           + (f"  [saved {saved}]" if saved else ""))
        return True
    _p(f"    captcha not solved after {attempts} attempts")
    return False


def _fresh_links(client: HttpClient, cfg, detail_url: str) -> dict[str, str]:
    """Re-parse the detail page in the verified session for working links.

    Document links carry a per-render ``sp=`` token; tokens minted before the
    session was captcha-verified are stale, so we re-fetch the detail page once
    the session is unlocked to obtain links that stream directly (this also
    yields the 'Download as zip file' bundle).
    """
    parsed = parse_detail(client.get(detail_url).text, base_url=cfg.base_url)
    return {d["filename"]: d["download_url"]
            for d in parsed["documents"] if d.get("download_url")}


def explode_zip(conn, cfg, tender_id: str, zip_path: Path, dest_dir: Path,
                progress=None) -> int:
    """Unpack a captured zip bundle into individual captured document rows.

    The 'Download as zip file' bundle contains the work-item files (BOQ, technical
    docs) whose individual rows are otherwise 'lost'/'missing'. Each member is
    written to disk and matched to its existing document row by filename (marking
    it captured), or inserted as a new captured doc — so every file is
    individually downloadable, viewable and text-indexed.
    """
    import zipfile

    _p = progress or (lambda msg: log.info(msg))
    added = 0
    try:
        zf = zipfile.ZipFile(zip_path)
    except Exception as exc:  # noqa: BLE001 - a genuinely unreadable bundle
        log.warning("explode zip %s unreadable: %s", zip_path, exc)
        return 0
    with zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        existing = {r["filename"]: r["id"] for r in conn.execute(
            "SELECT id, filename FROM documents WHERE tender_id=?", (tender_id,))}
        existing_lc = {k.lower(): v for k, v in existing.items()}
        sub = dest_dir / "_unzipped"
        sub.mkdir(parents=True, exist_ok=True)
        for m in members:
            inner = Path(m.filename).name
            if not inner:
                continue
            # Per-member isolation: one unreadable entry (or one locked write)
            # must not abandon the remaining files. The old blanket try/except
            # around the whole loop swallowed "database is locked" and dropped
            # every member silently — irreplaceable data lost to a log line.
            try:
                data = zf.read(m)
                target = sub / safe_filename(inner)
                doc_id = existing.get(inner) or existing_lc.get(inner.lower())
                if doc_id is None:
                    conn.execute(
                        "INSERT OR IGNORE INTO documents (tender_id, filename, section,"
                        " status, source, first_seen_at) VALUES (?, ?,"
                        " 'Extracted from zip', 'pending', 'scraped', ?)",
                        (tender_id, inner, now_iso()))
                    got = conn.execute(
                        "SELECT id FROM documents WHERE tender_id=? AND filename=?"
                        " AND section='Extracted from zip'", (tender_id, inner)).fetchone()
                    if got is None:
                        continue
                    doc_id = got["id"]
                    existing[inner] = doc_id
                store_document(conn, cfg, doc_id, target, data, None)
                conn.commit()
                added += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("explode zip %s member %s failed: %s",
                            zip_path, inner, exc)
                _p(f"    ↳ ✗ {inner}: {exc}")
    if added:
        _p(f"    ↳ unzipped {added} file(s) from {zip_path.name}")
    return added


def explode_existing_zips(db_path=None, *, limit: int = 0) -> dict:
    """Backfill: unpack every already-captured zip bundle into individual docs."""
    from .config import load_config
    from .db import connect, init_db

    cfg = load_config()
    db_path = db_path or cfg.db_path
    init_db(db_path)
    conn = connect(db_path)
    root = cfg.db_path.parent.parent
    zips = files = 0
    try:
        sql = ("SELECT tender_id, stored_path FROM documents WHERE status='captured' "
               "AND lower(filename) LIKE '%.zip' AND stored_path IS NOT NULL")
        rows = conn.execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
        for r in rows:
            zp = root / r["stored_path"]
            if not zp.exists():
                continue
            n = explode_zip(conn, cfg, r["tender_id"], zp, zp.parent)
            if n:
                zips += 1
                files += n
    finally:
        conn.close()
    return {"zips": zips, "files_added": files}


def _fail(conn, row, tender_id: str, reason: str) -> None:
    """Record a failed capture attempt, honouring the retry schedule.

    A captured document being *re-checked* for modification must never be
    downgraded: its bytes are already safe on disk and a transient portal error
    says nothing about them. Only its next re-check is pushed out.
    """
    if row["status"] == "captured":
        conn.execute("UPDATE documents SET recheck_at=? WHERE id=?",
                     (next_recheck_after(), row["id"]))
        return
    note_download_failed(conn, row["id"], tender_id, row["attempts"] or 0, reason)


def download_for_tender(conn, client: HttpClient, cfg, tender_id: str,
                        detail_url: str | None = None, progress=None,
                        recheck_budget: int = 0) -> dict:
    """Capture this tender's outstanding documents, and optionally re-check some.

    Two row sets are fetched in one captcha-verified pass:

    * **missing** — pending/failed rows whose retry schedule says they are due.
      Undue rows are skipped entirely; that is what keeps an unavailable file
      from being probed on every cycle forever.
    * **re-check** — already-captured rows whose bytes are old enough (or whose
      declared size changed on the detail page) to be worth re-downloading and
      hash-comparing, so a quietly re-published document is caught. Bounded by
      ``recheck_budget`` because this is the expensive half.
    """
    _p = progress or (lambda msg: log.info(msg))
    missing = conn.execute(
        "SELECT id, filename, section, download_url, status, attempts FROM documents "
        f"WHERE tender_id=? AND status IN ('pending','failed') AND {due_clause('documents')} "
        "AND download_url IS NOT NULL",
        (tender_id,),
    ).fetchall()
    rechecks = []
    if recheck_budget > 0:
        rechecks = conn.execute(
            "SELECT id, filename, section, download_url, status, attempts FROM documents d"
            " WHERE tender_id=? AND status='captured' AND download_url IS NOT NULL"
            "   AND (recheck_at IS NULL OR datetime(recheck_at) <= datetime('now'))"
            # Explicitly-triggered re-checks (a changed declared size) carry a
            # past timestamp and must outrank the blind never-checked sweep.
            " ORDER BY (recheck_at IS NULL), recheck_at LIMIT ?",
            (tender_id, recheck_budget),
        ).fetchall()
    rows = list(missing) + list(rechecks)
    if not rows:
        return {"captured": 0, "failed": 0, "rechecked": 0, "modified": 0}

    dest_dir = Path(cfg.docs_dir) / tender_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    captured = failed = rechecked = modified = 0

    # Solve the captcha once for the whole session, then fetch fresh links.
    if not _verify_session(client, cfg, rows[0]["download_url"], progress=_p):
        for row in rows:
            _fail(conn, row, tender_id, "captcha_unsolved")
        conn.commit()
        return {"captured": 0, "failed": len(missing), "rechecked": 0,
                "modified": 0, "reason": "captcha_unsolved"}

    link_map: dict[str, str] = {}
    if detail_url:
        try:
            link_map = _fresh_links(client, cfg, detail_url)
        except Exception as exc:  # noqa: BLE001
            log.debug("fresh-link refetch failed for %s: %s", tender_id, exc)
    # Fall back to stored links (valid for tenders fetched while already verified).
    for row in rows:
        link_map.setdefault(row["filename"], row["download_url"])

    for row in rows:
        is_recheck = row["status"] == "captured"
        url = link_map.get(row["filename"])
        if not url:
            _fail(conn, row, tender_id, "no_download_link")
            if not is_recheck:
                failed += 1
                _p(f"    {row['filename']}  ✗ no download link")
            continue
        try:
            _p(f"    {row['filename']}  {'re-checking' if is_recheck else 'downloading'}…")
            resp = client.get(url)
            content = resp.content
            ctype = resp.headers.get("Content-Type", "")
            conn.execute(
                "INSERT INTO fetch_log (url, tender_id, http_status, kind, fetched_at)"
                " VALUES (?, ?, ?, 'document', ?)",
                (url, tender_id, resp.status_code, now_iso()),
            )
            if resp.status_code != 200 or _looks_like_html(content, ctype):
                _fail(conn, row, tender_id, f"http_{resp.status_code}")
                if not is_recheck:
                    failed += 1
                    _p(f"    {row['filename']}  ✗ bad response ({resp.status_code})")
                conn.commit()
                continue
            # Prefer the server's own filename (gives the real zip name).
            fname = safe_filename(_disposition_name(resp) or row["filename"])
            target = dest_dir / fname
            outcome = store_document(conn, cfg, row["id"], target, content, ctype)
            size_kb = len(content) // 1024
            if is_recheck:
                rechecked += 1
                if outcome == "modified":
                    modified += 1
                    _p(f"    {fname}  ⟳ MODIFIED, previous version preserved")
            else:
                captured += 1
                _p(f"    {fname}  ✓ {size_kb} KB")
            # A zip bundle holds the otherwise-missing work-item files; unpack it.
            # Unchanged bundles are skipped: their members cannot have changed.
            if outcome != "unchanged" and (fname.lower().endswith(".zip")
                                           or "zip" in (ctype or "").lower()):
                captured += explode_zip(conn, cfg, tender_id, target, dest_dir, _p)
        except Exception as exc:  # noqa: BLE001
            log.warning("download failed %s/%s: %s", tender_id, row["filename"], exc)
            _fail(conn, row, tender_id, str(exc)[:200])
            if not is_recheck:
                failed += 1
            _p(f"    {row['filename']}  ✗ {exc}")
        conn.commit()
    return {"captured": captured, "failed": failed, "rechecked": rechecked,
            "modified": modified}


class VersionPreservationError(RuntimeError):
    """Raised when a superseded file could not be safely copied aside.

    Losing the previous version is not an acceptable outcome, so the new bytes
    are refused rather than written over an unpreserved predecessor.
    """


def _preserve_previous(conn, cfg, row, doc_id: int) -> str | None:
    """Copy the currently-stored file aside and record it in document_versions.

    Copy, never move: the live ``stored_path`` stays valid for the web app and
    for anything already linking to it, and nothing under data/docs is removed.
    The old bytes are re-hashed from disk rather than trusted from the DB, so the
    version row describes what was actually archived.
    """
    root = cfg.db_path.parent.parent
    old_rel = row["stored_path"]
    if not old_rel:
        return None
    old_path = root / old_rel
    if not old_path.exists():
        # Nothing to preserve; the integrity pass will already have flagged this.
        return None

    old_bytes_sha = hashlib.sha256()
    with open(old_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            old_bytes_sha.update(chunk)
    old_sha = old_bytes_sha.hexdigest()

    versions = old_path.parent / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9T]", "", (row["downloaded_at"] or now_iso()))[:15]
    vpath = versions / f"{stamp}_{old_sha[:12]}_{old_path.name}"
    if not vpath.exists():
        shutil.copy2(old_path, vpath)
    if vpath.stat().st_size != old_path.stat().st_size:
        raise VersionPreservationError(f"short copy of {old_path} -> {vpath}")

    conn.execute(
        "INSERT OR IGNORE INTO document_versions (document_id, tender_id, filename,"
        " sha256, byte_size, stored_path, content_type, captured_at, superseded_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_id, row["tender_id"], row["filename"], old_sha,
         vpath.stat().st_size, str(vpath.relative_to(root)), row["content_type"],
         row["downloaded_at"], now_iso()),
    )
    return old_sha


def store_document(conn, cfg, doc_id: int, target: Path, content: bytes,
                   content_type: str | None) -> str:
    """Write captured bytes for a document, versioning any predecessor.

    Returns 'new' | 'unchanged' | 'modified'. A re-published document is the
    whole point of the version chain — a specification quietly edited mid-bid is
    what this archive exists to catch — so the previous file is copied aside
    *before* anything is written.
    """
    sha = hashlib.sha256(content).hexdigest()
    row = conn.execute(
        "SELECT id, tender_id, filename, sha256, stored_path, content_type,"
        " downloaded_at, status FROM documents WHERE id=?", (doc_id,)).fetchone()
    prev_sha = row["sha256"] if row else None

    outcome = "new"
    if prev_sha and prev_sha == sha:
        outcome = "unchanged"
    elif prev_sha:
        _preserve_previous(conn, cfg, row, doc_id)
        outcome = "modified"

    now = now_iso()
    if outcome == "unchanged":
        # Nothing was written, so stored_path must keep pointing at the file
        # that actually exists — re-checks are told the bytes are still good and
        # when to look again, and nothing else about the row moves.
        conn.execute(
            "UPDATE documents SET verified_at=?, recheck_at=?, attempts=0,"
            " next_attempt_at=NULL, last_attempt_at=? WHERE id=?",
            (now, next_recheck_after(), now, doc_id),
        )
        return outcome

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    rel = str(target.relative_to(cfg.db_path.parent.parent))
    conn.execute(
        "UPDATE documents SET status='captured', stored_path=?, byte_size=?,"
        " sha256=?, content_type=?, downloaded_at=?, attempts=0,"
        " next_attempt_at=NULL, last_attempt_at=?, verified_at=?, recheck_at=?,"
        " version_count=version_count + ? WHERE id=?",
        (rel, len(content), sha, content_type, now, now, now,
         next_recheck_after(), 1 if outcome == "modified" else 0, doc_id),
    )
    if outcome == "modified":
        # Stale extracted text would otherwise keep describing the old file;
        # dropping the row re-queues it for the next extraction pass.
        conn.execute("DELETE FROM doc_text WHERE document_id=?", (doc_id,))
        record_event(conn, doc_id, row["tender_id"], "version_captured",
                     f"{prev_sha[:12]} -> {sha[:12]}", at=now)
    elif outcome == "new":
        record_event(conn, doc_id, row["tender_id"] if row else None,
                     "captured", sha[:12], at=now)
    return outcome


def _mark_captured(conn, doc_id, target: Path, content: bytes, cfg, content_type):
    """Back-compat shim: write + record, versioning any predecessor."""
    return store_document(conn, cfg, doc_id, target, content, content_type)
