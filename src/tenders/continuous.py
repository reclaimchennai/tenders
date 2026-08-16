"""Continuous capture daemon.

Runs the capture pipeline in an endless, resilient loop so the mirror keeps
itself current without manual runs:

* **every few minutes, throughout** — poll page 1 of the newest-published-first
  "Latest Active Tenders" listing and immediately capture anything that closes
  within hours (see latest_active). This is the only pass fast enough to see a
  tender whose whole life is one afternoon, so it is ticked from *inside* the
  cycle below as well as during the pause; a cycle can run for hours, and a
  detector that waited for one would be no detector at all.
* **every cycle** — enumerate active tenders (organisation tree), then detail +
  download **all** outstanding documents (no per-run limit), then extract text
  and refresh the search index. This is exactly one ``run_forward`` pass.
* **every cycle, bounded** — audit a slice of already-captured documents against
  their stored hashes, skipping anything checked in the last 12 hours. Sliced
  rather than scheduled as one 12-hourly job because hashing several gigabytes
  in a single pass would stall a cycle; the age filter is what actually makes
  the period 12 hours.
* **every Nth cycle** — also enumerate Cancelled + Retendered tenders (captcha-
  gated, slower-changing) and capture their surviving documents.

Each cycle uses a fresh HTTP session, so the document captcha is solved at most
once per cycle and then reused for every tender that cycle. Exceptions in a
cycle are logged and swallowed — the daemon must never die on a transient error.
A short pause separates cycles; when everything is already captured a cycle is
cheap (enumeration finds nothing new) and the pause dominates, keeping load on
the portal low.
"""

from __future__ import annotations

import logging
import signal
import time

from .config import load_config
from .forward_capture import run_forward

log = logging.getLogger("continuous")

_stop = False


def _handle_signal(signum, _frame):  # pragma: no cover - signal path
    global _stop
    _stop = True
    log.info("received signal %s; finishing current cycle then stopping", signum)


def _pause(db_path, seconds: int, watch, retry) -> None:
    """Sleep between cycles, still polling for short-window tenders and due
    capture retries.

    Sliced small so a signal interrupts promptly. The poll gets its own session
    here because no cycle is running to lend it one; that costs a session
    bootstrap per poll and buys a fresh JSESSIONID, which an idle session would
    have lost anyway.
    """
    slept = 0
    while slept < seconds and not _stop:
        due = (watch is not None and watch.due()) or (retry is not None and retry.due())
        if due:
            from .db import connect
            from .http_client import HttpClient

            conn = connect(db_path)
            try:
                client = HttpClient(load_config())
                if watch is not None:
                    watch.tick(conn, client)
                if retry is not None:
                    retry.tick(conn, client)
            finally:
                conn.close()
        time.sleep(min(5, seconds - slept))
        slept += 5


def run_continuous(db_path=None, *, cycle_pause_s: int = 900,
                   cancelled_every: int = 6, once: bool = False,
                   verify_batch: int = 500, verify_max_age_h: int = 12,
                   latest_interval_s: int | None = None) -> dict:
    """Loop the capture pipeline until interrupted (or one cycle if ``once``).

    cycle_pause_s      seconds to sleep between cycles when work is caught up.
    cancelled_every    run cancelled/retender enumeration every N cycles (0 = never).
    verify_batch       captured documents re-validated per cycle (0 = never).
    verify_max_age_h   leave documents alone if verified this recently.
    latest_interval_s  seconds between short-window polls (0 = disable).
    """
    cfg = load_config()
    db_path = db_path or cfg.db_path

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    from .capture_retry import CaptureRetryWatch
    from .capture_retry import settings as retry_settings
    from .latest_active import LatestWatch, settings

    opts = settings(cfg)
    if latest_interval_s is None:
        latest_interval_s = int(opts["poll_interval_s"]) if opts["enabled"] else 0
    watch = (LatestWatch(cfg, interval_s=latest_interval_s, progress=log.info)
             if latest_interval_s else None)
    if watch is None:
        log.warning("short-window polling is DISABLED; sub-day tenders will be missed")

    ropts = retry_settings(cfg)
    retry = CaptureRetryWatch(cfg, progress=log.info) if ropts["enabled"] else None
    if retry is None:
        log.warning("progressive capture retry is DISABLED; new tenders wait"
                    " for the full cycle instead of 1m/5m/15m/30m/60m")

    cycle = 0
    totals = {"cycles": 0, "docs_captured": 0}
    while not _stop:
        cycle += 1
        totals["cycles"] = cycle
        log.info("=== cycle %d starting ===", cycle)
        try:
            res = run_forward(db_path, watch=watch, retry=retry)
            totals["docs_captured"] += res.get("docs_captured", 0)
            log.info("cycle %d active: detailed=%s captured=%s failed=%s",
                     cycle, res.get("detailed"), res.get("docs_captured"),
                     res.get("docs_failed"))
        except Exception as exc:  # noqa: BLE001
            log.warning("cycle %d forward pass failed: %s", cycle, exc)

        # Fill in concise short names for newly-seen tenders (bounded per cycle).
        try:
            from .shortnames import run_shortnames

            sn = run_shortnames(db_path, limit=80)
            if sn.get("filled"):
                log.info("cycle %d short names: +%s (%s remaining)",
                         cycle, sn["filled"], sn.get("remaining"))
        except Exception as exc:  # noqa: BLE001
            log.warning("cycle %d shortnames pass failed: %s", cycle, exc)

        if verify_batch:
            try:
                from .integrity import run_verify

                iv = run_verify(db_path, limit=verify_batch,
                                max_age_hours=verify_max_age_h)
                if iv.get("checked"):
                    log.info("cycle %d integrity: checked=%s valid=%s requeued=%s %s",
                             cycle, iv["checked"], iv["valid"], iv["requeued"],
                             iv["reasons"] or "")
            except Exception as exc:  # noqa: BLE001
                log.warning("cycle %d integrity pass failed: %s", cycle, exc)

        if cancelled_every and cycle % cancelled_every == 1:
            try:
                from .enumerate_cancelled import run_cancelled

                cres = run_cancelled(db_path, download=True)
                totals["docs_captured"] += cres.get("docs_captured", 0)
                log.info("cycle %d cancelled: found=%s detailed=%s captured=%s",
                         cycle, cres.get("found"), cres.get("detailed"),
                         cres.get("docs_captured"))
            except Exception as exc:  # noqa: BLE001
                log.warning("cycle %d cancelled pass failed: %s", cycle, exc)

        if watch is not None:
            log.info("cycle %d short-window watch: %s", cycle, watch.totals)
            totals["latest"] = dict(watch.totals)

        if retry is not None:
            log.info("cycle %d capture retry: %s", cycle, retry.totals)
            totals["capture_retry"] = dict(retry.totals)

        if once or _stop:
            break

        log.info("=== cycle %d done; sleeping %ds ===", cycle, cycle_pause_s)
        _pause(db_path, cycle_pause_s, watch, retry)

    log.info("continuous capture stopped after %d cycle(s)", cycle)
    return totals
