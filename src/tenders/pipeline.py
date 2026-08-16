"""Detail-fetch pipeline: fetch -> archive raw HTML -> parse -> store.

Advances tenders from any status to 'detailed' (or 'failed'), writing clean,
authoritative metadata and document rows parsed from the live detail page.
Optionally downloads live documents in the same pass.
"""

from __future__ import annotations

import gzip
import json
import logging

from .config import load_config
from .db import connect, init_db
from .doc_lifecycle import note_link_appeared, note_missing, record_event
from .http_client import HttpClient
from .parse_detail import FIELD_MAP, cancellation, parse_detail
from .redflags import check_and_flag
from .util import now_iso, parse_date, parse_money

log = logging.getLogger("pipeline")


def normalise_detail_url(url: str | None) -> str | None:
    """Repair ``FrontEndViewTender`` permalinks minted with the wrong component.

    ``parse_listing.permalink`` rewrites a listing row's ``page=`` to name the
    detail page but leaves ``component=`` as the listing wrote it. Every other
    surface writes ``component=$DirectLink``, but ``WebTenderStatusLists`` — the
    surface both historical backfills run on — writes ``component=view``, and
    the portal answers that pair with "Your session in the client area has
    expired" rather than the tender. Same ``sp`` token, same page, no data.

    That silently disabled recovery for the 15,328 dateless tenders those
    backfills found, which is the entire point of having stored a permalink for
    them, so the shape is corrected here at the moment of use as well as at the
    source.
    """
    if not url or "FrontEndViewTender" not in url:
        return url
    return url.replace("component=view&", "component=%24DirectLink&")


def archive_html(cfg, tender_id: str, html: str) -> str:
    ts = now_iso().replace(":", "").replace("-", "")
    d = cfg.html_dir / tender_id
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{ts}.html.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(html)
    return str(path.relative_to(cfg.db_path.parent.parent))


def _apply_fields(conn, tender_id: str, parsed: dict, html_path: str) -> None:
    fields = parsed["fields"]
    corrigenda = parsed.get("corrigenda") or []
    cols: dict = {"tender_id": tender_id}
    for label, (base, is_date, is_money) in FIELD_MAP.items():
        val = fields.get(label)
        if val in (None, "", "NA"):
            val = None
        if is_date:
            cols[base] = parse_date(val)
            cols[f"{base}_raw"] = val
        elif is_money:
            cols[f"{base}_raw"] = val
            if base == "tender_value":
                cols["tender_value_num"] = parse_money(val)
        else:
            cols[base] = val

    cols["raw_json"] = json.dumps(fields, ensure_ascii=False)
    cols["corrigenda_json"] = json.dumps(corrigenda, ensure_ascii=False)
    cols["corrigendum_count"] = len(corrigenda)
    cols["detail_html_path"] = html_path
    cols["status"] = "detailed"
    cols["source"] = "scraped"
    cols["last_updated_at"] = now_iso()

    sets = ", ".join(f"{k} = :{k}" for k in cols if k != "tender_id")
    # A cancellation corrigendum is the only cancellation signal an ordinary
    # detail page carries. It is recorded sticky (COALESCE, never cleared): the
    # portal can and does drop corrigendum rows, and an archive that quietly
    # un-cancels a tender because a table changed is worse than useless.
    cancel = cancellation(corrigenda)
    cols["cancelled_at"] = now_iso() if cancel else None
    cols["cancellation_note"] = (cancel or {}).get("title") if cancel else None
    sets += (", cancelled_at = COALESCE(cancelled_at, :cancelled_at)"
             ", cancellation_note = COALESCE(cancellation_note, :cancellation_note)")

    # Ensure the row exists (it normally does from discovery/CSV).
    conn.execute(
        "INSERT INTO tenders (tender_id, first_seen_at, last_updated_at) "
        "VALUES (:tender_id, :ts, :ts) ON CONFLICT(tender_id) DO NOTHING",
        {"tender_id": tender_id, "ts": now_iso()},
    )
    conn.execute(f"UPDATE tenders SET {sets} WHERE tender_id = :tender_id", cols)

    # The detail page is the first point at which the bid-submission and
    # advertisement dates exist, so this is where a short-window red flag
    # raised provisionally from a listing row is confirmed — or retracted,
    # if those dates prove the window was never short. Re-read the stored row
    # rather than using `cols`: published_date/closing_date are COALESCEd above
    # and the merged values are what the flag must be judged on.
    row = conn.execute(
        "SELECT published_date, closing_date, raw_json FROM tenders"
        " WHERE tender_id = ?", (tender_id,)).fetchone()
    if row is not None:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, ValueError):
            raw = {}
        check_and_flag(conn, tender_id, row["published_date"], row["closing_date"],
                       raw=raw if isinstance(raw, dict) else {})


def _rejection_reason(fields: dict, tender_id: str) -> str | None:
    """Why this parse must not be written, or None if it is a real detail page.

    An ``sp`` token is an opaque tender key we never mint ourselves, so a token
    that resolves to a *different* tender means the row's provenance is wrong.
    Silently merging that tender's value and dates into this one would fabricate
    a record, which is the one thing an evidence archive may never do.
    """
    if not fields.get("Tender ID") and not fields.get("Organisation Chain"):
        return "no tender fields on page"
    stated = (fields.get("Tender ID") or "").strip()
    if stated and stated != tender_id:
        return f"page is for {stated}"
    return None


def _apply_documents(conn, tender_id: str, documents: list[dict]) -> None:
    """Reconcile the parsed document list against stored rows.

    Beyond the upsert this records the *transitions* the retry schedule and the
    publish-lag report are built on (see doc_lifecycle): a document seen without
    a download link, and a link later appearing for one. The prior row therefore
    has to be read before it is overwritten — the old single-statement upsert
    could not tell "still missing" from "just went missing".
    """
    # A live scrape is the authoritative, clean document list. Drop the
    # low-confidence CSV-recovered rows (mangled filenames) for this tender.
    conn.execute("DELETE FROM documents WHERE tender_id=? AND source='csv'", (tender_id,))
    now = now_iso()
    for d in documents:
        has_link = bool(d.get("download_url"))
        status = "pending" if has_link else "lost"
        prev = conn.execute(
            "SELECT id, status, declared_size, link_first_seen_at,"
            " first_seen_missing_at, attempts FROM documents"
            " WHERE tender_id=? AND filename=? AND section IS ?",
            (tender_id, d["filename"], d.get("section")),
        ).fetchone()

        conn.execute(
            """
            INSERT INTO documents
                (tender_id, filename, section, description, declared_size,
                 download_url, status, source, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'scraped', ?)
            ON CONFLICT(tender_id, filename, section) DO UPDATE SET
                description   = excluded.description,
                declared_size = excluded.declared_size,
                download_url  = excluded.download_url,
                source        = 'scraped',
                first_seen_at = COALESCE(documents.first_seen_at, excluded.first_seen_at),
                -- never downgrade a captured doc back to lost/pending
                status = CASE WHEN documents.status='captured'
                              THEN 'captured' ELSE excluded.status END
            """,
            (
                tender_id,
                d["filename"],
                d.get("section"),
                d.get("description"),
                d.get("declared_size"),
                d.get("download_url"),
                status,
                now,
            ),
        )

        if prev is None:
            row = conn.execute(
                "SELECT id FROM documents WHERE tender_id=? AND filename=? AND section IS ?",
                (tender_id, d["filename"], d.get("section")),
            ).fetchone()
            if row is None:  # pragma: no cover - the insert above just ran
                continue
            doc_id = row["id"]
            if has_link:
                conn.execute(
                    "UPDATE documents SET link_first_seen_at=? WHERE id=?",
                    (now, doc_id))
            else:
                note_missing(conn, doc_id, tender_id, None, 0, first_time=True)
            continue

        doc_id = prev["id"]
        if prev["status"] == "captured":
            # The detail page's own size column is a free change signal: no
            # request of ours is needed to notice a re-published file, so a
            # changed size short-circuits the (expensive) 12-hourly re-download.
            new_size = d.get("declared_size")
            if new_size and prev["declared_size"] and new_size != prev["declared_size"]:
                conn.execute("UPDATE documents SET recheck_at=? WHERE id=?", (now, doc_id))
                record_event(conn, doc_id, tender_id, "declared_size_changed",
                             f"{prev['declared_size']} -> {new_size}", at=now)
            continue

        if has_link:
            # Only a genuine missing -> present transition clears the retry
            # budget. A 'failed' row already has a link; resetting its counter
            # here would erase the download backoff and re-probe every cycle.
            if prev["status"] == "lost" or not prev["link_first_seen_at"]:
                note_link_appeared(conn, doc_id, tender_id)
        else:
            note_missing(conn, doc_id, tender_id, prev["first_seen_missing_at"],
                         prev["attempts"] or 0,
                         first_time=prev["first_seen_missing_at"] is None)


def fetch_and_store_detail(conn, client: HttpClient, cfg, tender_id: str, url: str,
                           download: bool = False, progress=None,
                           recheck_budget: int = 0) -> dict:
    _p = progress or (lambda *_: None)
    fixed = normalise_detail_url(url)
    if fixed != url:
        conn.execute(
            "UPDATE tenders SET detail_url=? WHERE tender_id=? AND detail_url=?",
            (fixed, tender_id, url))
        url = fixed
    resp = client.get(url)
    html = resp.text
    html_path = archive_html(cfg, tender_id, html)
    conn.execute(
        "INSERT INTO fetch_log (url, tender_id, http_status, kind, html_path, fetched_at)"
        " VALUES (?, ?, ?, 'detail', ?, ?)",
        (url, tender_id, resp.status_code, html_path, now_iso()),
    )
    if resp.status_code != 200:
        conn.execute(
            "UPDATE tenders SET status='failed', last_updated_at=? WHERE tender_id=?",
            (now_iso(), tender_id),
        )
        conn.commit()
        return {"tender_id": tender_id, "ok": False, "http": resp.status_code}

    parsed = parse_detail(html, base_url=cfg.base_url)
    # The portal answers several failure modes with a 200 and a page that has no
    # caption/field grid at all — a session-expired notice, a maintenance stub.
    # Writing that parse would overwrite a tender's recovered title, reference
    # number and organisation chain with NULL and then mark it 'detailed', so
    # the loss would never be retried. Refuse it, and fail loudly instead.
    bad = _rejection_reason(parsed["fields"], tender_id)
    if bad:
        conn.execute(
            "UPDATE tenders SET status='failed', last_updated_at=? WHERE tender_id=?",
            (now_iso(), tender_id),
        )
        conn.commit()
        log.warning("detail page for %s rejected (%s); metadata left intact",
                    tender_id, bad)
        return {"tender_id": tender_id, "ok": False, "rejected": bad}

    _apply_fields(conn, tender_id, parsed, html_path)
    _apply_documents(conn, tender_id, parsed["documents"])
    conn.commit()

    n_live = sum(1 for d in parsed["documents"] if d.get("download_url"))
    result = {
        "tender_id": tender_id,
        "ok": True,
        "documents": len(parsed["documents"]),
        "live_documents": n_live,
    }

    if download and n_live:
        from .download_docs import download_for_tender

        result["downloaded"] = download_for_tender(
            conn, client, cfg, tender_id, detail_url=url, progress=_p,
            recheck_budget=recheck_budget)
    return result


def run_detail(db_path=None, *, limit: int = 50, tender_id: str | None = None,
               download: bool = False) -> dict:
    from tqdm import tqdm

    cfg = load_config()
    db_path = db_path or cfg.db_path
    cfg.ensure_dirs()
    init_db(db_path)
    conn = connect(db_path)
    client = HttpClient(cfg)
    processed: list[dict] = []
    try:
        if tender_id:
            rows = conn.execute(
                "SELECT tender_id, detail_url FROM tenders WHERE tender_id=?",
                (tender_id,),
            ).fetchall()
        else:
            # limit <= 0 means "process every discovered tender".
            sql = ("SELECT tender_id, detail_url FROM tenders "
                   "WHERE detail_url IS NOT NULL AND status='discovered' "
                   "ORDER BY first_seen_at")
            rows = (conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
                    if limit and limit > 0 else conn.execute(sql).fetchall())
        rows = [r for r in rows if r["detail_url"]]
        bar = tqdm(rows, unit="tender", ncols=90,
                   bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]")
        for row in bar:
            tid = row["tender_id"]
            bar.set_description(f"{tid[:30]}")
            try:
                result = fetch_and_store_detail(
                    conn, client, cfg, tid, row["detail_url"],
                    download=download, progress=tqdm.write,
                )
                processed.append(result)
                n_live = result.get("live_documents", 0)
                dl = result.get("downloaded") or {}
                if download and n_live:
                    cap = dl.get("captured", 0)
                    fail = dl.get("failed", 0)
                    reason = f"  ↳ {n_live} live docs → {cap} captured" + (
                        f", {fail} failed" if fail else "")
                else:
                    reason = f"  ↳ {result.get('documents', 0)} docs ({n_live} live)"
                tqdm.write(f"  {tid}  {reason}")
            except Exception as exc:  # noqa: BLE001
                log.warning("detail failed for %s: %s", tid, exc)
                tqdm.write(f"  {tid}  ✗ {exc}")
                processed.append({"tender_id": tid, "ok": False, "error": str(exc)})
    finally:
        conn.close()
    ok = sum(1 for r in processed if r.get("ok"))
    tqdm.write(f"\nDone: {ok}/{len(processed)} ok, {client.request_count} requests")
    return {"processed": len(processed), "ok": ok,
            "results": processed[:20], "requests": client.request_count}
