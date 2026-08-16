"""Resumable, captcha-free enumeration of tenders via the organisation tree.

"Tenders by Organisation" exposes a tree of organisations (each with a tender
count) whose drill-down links are plain session-bound GET links — no captcha,
unlike the keyword search. Walking the tree to its leaves yields every currently
listed tender's stable permalink and Tender ID, which we store as 'discovered'
for the detail pipeline to enrich.

Discovery is idempotent (ON CONFLICT DO NOTHING on Tender ID), so re-runs only
add genuinely new tenders. Drill links are session-bound, so the whole walk runs
inside one HttpClient session.
"""

from __future__ import annotations

import logging

from .config import load_config
from .db import connect, init_db
from .http_client import HttpClient, RequestCapExceeded
from .parse_listing import parse_org_tree, parse_tender_list
from .util import now_iso

log = logging.getLogger("enumerate")

MAX_DEPTH = 6


def _record_tenders(conn, tenders: list[dict]) -> int:
    """Insert newly discovered tenders. Returns the count of genuinely new rows.

    Shares the upsert with the newest-first poll (latest_active.record_row)
    because the two surfaces return identical rows and both carry the dates a
    short-bid-window flag needs. Discovery used to store only id/title/url here,
    which is why a tender sat undateable — and so unflaggable — until the detail
    queue eventually reached it, hours or days later.
    """
    from .latest_active import record_row

    new = 0
    for t in tenders:
        was_new, _ = record_row(conn, t)
        new += 1 if was_new else 0
    return new


def _walk(conn, client: HttpClient, cfg, start_url: str, *, tick=None) -> dict:
    """Depth-first drill of the organisation tree.

    ``tick`` is called once per fetched page. The walk is hundreds of requests
    long, which is long enough for a short-window tender to be published and
    closed inside it, so the newest-first poll needs a chance to run in here
    rather than waiting for the walk to end (see latest_active.LatestWatch).
    """
    from tqdm import tqdm

    host = cfg.host
    stack = [(start_url, 0)]
    visited: set[str] = set()
    orgs_seen = 0
    tenders_seen = 0

    # Unknown depth upfront — use a spinner bar that tracks requests made.
    bar = tqdm(desc="Walking org tree", unit="req", ncols=90)
    try:
        while stack:
            url, depth = stack.pop()
            if url in visited or depth > MAX_DEPTH:
                continue
            visited.add(url)
            bar.update(1)
            bar.set_postfix(orgs=orgs_seen, tenders=tenders_seen, refresh=False)
            try:
                resp = client.get(url)
            except RequestCapExceeded:
                tqdm.write("request cap reached; stopping walk")
                break
            conn.execute(
                "INSERT INTO fetch_log (url, http_status, kind, fetched_at) "
                "VALUES (?, ?, 'listing', ?)",
                (url, resp.status_code, now_iso()),
            )
            if resp.status_code != 200:
                continue
            html_text = resp.text

            tenders = parse_tender_list(html_text, host)
            if tenders:
                tenders_seen += len(tenders)
                _record_tenders(conn, tenders)
                conn.commit()
                tqdm.write(f"  +{len(tenders)} tenders  (total {tenders_seen})")

            # Recurse into sub-organisations (skip the root self-link).
            orgs = parse_org_tree(html_text, host)
            for org in orgs:
                if org["drill_url"] not in visited:
                    orgs_seen += 1
                    stack.append((org["drill_url"], depth + 1))

            if tick is not None:
                tick(conn, client)
    finally:
        bar.close()

    return {"orgs_visited": orgs_seen, "tenders_discovered": tenders_seen,
            "requests": client.request_count}


def run_backfill(db_path=None, *, listing: str = "both", max_pages: int = 0) -> dict:
    cfg = load_config()
    db_path = db_path or cfg.db_path
    cfg.ensure_dirs()
    init_db(db_path)
    conn = connect(db_path)
    client = HttpClient(cfg)
    results: dict = {}
    try:
        if listing in ("active", "both"):
            log.info("enumerating ACTIVE tenders via organisation tree")
            results["active"] = _walk(conn, client, cfg, cfg.active_org_url)
        if listing in ("archive", "both"):
            log.info("enumerating ARCHIVE tenders")
            results["archive"] = _walk(conn, client, cfg, cfg.archive_page)
        # Mark crawl state.
        conn.execute(
            "INSERT INTO crawl_state (listing, complete, updated_at) VALUES (?, 1, ?) "
            "ON CONFLICT(listing) DO UPDATE SET complete=1, updated_at=excluded.updated_at",
            (listing, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return results
