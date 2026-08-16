"""Daily forward capture — the core of the project.

Runs the full pipeline against currently-active tenders so their documents are
saved before the portal deletes them on close:

1. Enumerate the organisation tree (captcha-free) to discover active tenders.
2. Fetch detail pages for tenders not yet detailed, prioritising those closing
   soonest (least time left to capture).
3. Download live documents (captcha-gated) in the same session.
4. Extract text and refresh the search index.

Steps 1-3 together run for hours once there is any backlog, which is far too
slow to notice a tender that opens and closes the same afternoon. The optional
``watch`` (latest_active.LatestWatch) is therefore ticked throughout the pass —
between org-tree pages and between tenders — so the newest-first poll keeps its
own much shorter period regardless of how long this pass takes. It shares this
pass's session and HTTP client, so it inherits the same politeness spacing and
adds no concurrency.

Idempotent and safe to run daily; already-captured work is skipped.
"""

from __future__ import annotations

import logging

from .config import load_config
from .db import connect, init_db
from .extract_text import run_extract
from .http_client import HttpClient, RequestCapExceeded
from .index_fts import rebuild_fts
from .pipeline import fetch_and_store_detail
from .util import now_iso

log = logging.getLogger("forward")


def run_forward(db_path=None, *, detail_limit: int = 0, watch=None,
                retry=None) -> dict:
    cfg = load_config()
    db_path = db_path or cfg.db_path
    cfg.ensure_dirs()
    init_db(db_path)

    # 1. Discover active tenders (own session; drill links are session-bound).
    from .enumerate_listings import _walk

    conn = connect(db_path)
    client = HttpClient(cfg)
    summary: dict = {"started_at": now_iso()}
    # watch (short-window discovery) and retry (progressive capture retry,
    # see capture_retry.py) are ticked together at every point this pass
    # already checks in — between org-tree pages and between tenders — so
    # both keep their own much shorter periods regardless of how long this
    # pass takes, the same reasoning as watch alone (see module docstring).
    tick = None
    if watch is not None or retry is not None:
        def tick(c, cl, force=False):
            if watch is not None:
                watch.tick(c, cl, force=force)
            if retry is not None:
                retry.tick(c, cl, force=force)
    try:
        # Poll before the walk as well as during it: on a fresh start the walk
        # is the longest single stretch of the pass, and anything published in
        # the last few minutes is cheapest to catch right now.
        if tick is not None:
            tick(conn, client, force=True)

        log.info("forward: enumerating active tenders")
        try:
            summary["enumeration"] = _walk(conn, client, cfg, cfg.active_org_url,
                                           tick=tick)
        except RequestCapExceeded:
            log.warning("request cap during enumeration")

        # 2. Detail + download, soonest-closing first. Three disjoint reasons to
        #    (re-)fetch a tender, all of which require it to be open except the
        #    first:
        #
        #    a) never detailed at all;
        #    b) still open and something is *known missing* and due for another
        #       probe. The portal routinely publishes a tender minutes before its
        #       document links exist, so a fast first scrape sees no link and
        #       pipeline marks those docs 'lost'; a re-fetch flips them back to
        #       'pending' once links appear (see _apply_documents). This clause
        #       previously ignored 'lost' entirely, so such a tender only came
        #       round on the 12-hourly sweep — hours of exposure on a document
        #       the portal deletes at close. It is now hourly, and bounded per
        #       document by next_attempt_at (see doc_lifecycle) so a file that is
        #       simply never going to appear is not probed forever;
        #    c) still open and not looked at for 12 hours — the catch-all that
        #       notices corrigenda, new documents and cancellations.
        #
        #    Closed tenders stay frozen: their documents are genuinely deleted
        #    and re-fetching only wastes the portal's bandwidth. A tender with no
        #    closing_date counts as open — an unknown deadline must not silently
        #    freeze a live tender out of the retry loop.
        #
        #    closing_date is naive IST wall-clock while last_updated_at is UTC,
        #    hence the +5:30 shift on 'now' for the open-ness test only.
        # A missing closing_date used to count as open, so that a live tender
        # with an unknown deadline could not be frozen out. That inverted once
        # the status-listing backfills landed: 62,334 register-only rows carry no
        # dates at all, so every one of them entered the *unbounded* live bucket,
        # the closed ration below never engaged, and a cycle grew to 80,813 rows
        # and ran for two days. Tenders published after such a cycle begins are
        # not in its row list, so they waited days for documents that the portal
        # deletes on close — the one failure this project exists to prevent.
        # Undated rows are now rationed with the rest of the recovery work, which
        # still reaches them, just not ahead of a tender closing tomorrow.
        is_open = ("(t.closing_date IS NOT NULL AND datetime(t.closing_date) >"
                   " datetime('now', '+5 hours', '+30 minutes'))")
        q = f"""
            SELECT DISTINCT t.tender_id, t.detail_url, {is_open} AS is_open
            FROM tenders t
            WHERE t.detail_url IS NOT NULL
              AND (
                t.status = 'discovered'
                OR ({is_open} AND EXISTS (
                      SELECT 1 FROM documents d
                      WHERE d.tender_id = t.tender_id
                        AND d.status IN ('pending','failed','lost')
                        AND (d.next_attempt_at IS NULL
                             OR datetime(d.next_attempt_at) <= datetime('now'))))
                OR ({is_open}
                    AND datetime(t.last_updated_at) < datetime('now', '-12 hours'))
              )
            ORDER BY
              -- Never captured beats already captured. Closing date alone put
              -- an open tender we have never read behind thousands of freshness
              -- re-checks of tenders whose documents are already on disk: the
              -- 12-hour clause sweeps in every open tender, so the live bucket
              -- is ~5,500 and a cycle is ~30 hours however small the fix above
              -- made it. Missing one of these is permanent — the portal deletes
              -- the documents at close — while a late re-check costs nothing,
              -- so the never-read ones go first and the rest fill the cycle.
              CASE
                WHEN t.status = 'discovered' THEN 0
                WHEN EXISTS (SELECT 1 FROM documents d
                             WHERE d.tender_id = t.tender_id
                               AND d.status IN ('pending','failed')) THEN 1
                ELSE 2
              END,
              (t.closing_date IS NULL), t.closing_date ASC
        """
        rows = conn.execute(q).fetchall()

        # Split the queue by urgency before anything is fetched. The historical
        # backfills discover tens of thousands of already-closed tenders whose
        # detail pages are worth recovering (metadata only — the documents are
        # deleted), and clause (a) sweeps every one of them into this list. Left
        # unbounded they push a cycle past ten hours, during which no live
        # tender's documents are captured at all — the archive's actual job.
        # So closed tenders get a per-cycle ration and open ones stay unbounded.
        backfill_cap = int(cfg.forward.get("backfill_detail_max_per_cycle", 400))
        live = [r for r in rows if r["is_open"]]
        closed = [r for r in rows if not r["is_open"]]
        rows = live + (closed[:backfill_cap] if backfill_cap else closed)
        if detail_limit:
            rows = rows[:detail_limit]
        log.info("forward: processing %d tenders (%d live, %d of %d closed"
                 " awaiting metadata recovery)",
                 len(rows), len(live), len(rows) - len(live), len(closed))

        # Modification detection needs the bytes back to compare hashes, which is
        # the most expensive thing we can do; a full 12-hourly sweep of every
        # open tender's captured documents would consume most of the politeness
        # budget on files that almost never change. So it is capped per run and
        # per tender, and the detail page's own size column (handled in
        # pipeline._apply_documents) triggers targeted re-checks for free.
        integrity_cfg = cfg.raw.get("integrity", {})
        recheck_left = int(integrity_cfg.get("recheck_max_per_run", 40))
        per_tender = int(integrity_cfg.get("recheck_max_per_tender", 5))

        captured = docs_failed = detailed = rechecked = modified = 0
        for row in rows:
            if tick is not None:
                tick(conn, client)
            budget = min(recheck_left, per_tender) if row["is_open"] else 0
            try:
                res = fetch_and_store_detail(conn, client, cfg, row["tender_id"],
                                             row["detail_url"], download=True,
                                             recheck_budget=budget)
                detailed += 1 if res.get("ok") else 0
                dl = res.get("downloaded") or {}
                captured += dl.get("captured", 0)
                docs_failed += dl.get("failed", 0)
                rechecked += dl.get("rechecked", 0)
                modified += dl.get("modified", 0)
                recheck_left -= dl.get("rechecked", 0)
            except RequestCapExceeded:
                log.warning("request cap reached; stopping capture loop")
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("capture failed for %s: %s", row["tender_id"], exc)
        summary.update(detailed=detailed, docs_captured=captured,
                       docs_failed=docs_failed, docs_rechecked=rechecked,
                       docs_modified=modified, requests=client.request_count)
        if watch is not None:
            summary["latest"] = dict(watch.totals)
        if retry is not None:
            summary["capture_retry"] = dict(retry.totals)
    finally:
        conn.close()

    # 3. Text extraction + index refresh.
    summary["extract"] = run_extract(db_path)
    summary["index"] = rebuild_fts(db_path)
    summary["finished_at"] = now_iso()
    log.info("forward done: %s", summary)
    return summary
