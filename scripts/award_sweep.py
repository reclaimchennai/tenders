"""Full retrospective sweep for Award-of-Contract documents.

Every awarded tender's AOC PDF is one request against a token we already hold,
and those PDFs are the richest evidence the portal publishes: the generated
comparative statements name every bidder, their quoted amount and rank, the
department's own estimate and the percentage above or below it. Unlike ordinary
tender documents — which the portal deletes at close, and which are simply gone
for the outage period — these survive, so this is recoverable history.

Run as a chained systemd unit rather than a bare loop because it is ~30 hours of
work: it waits for any in-flight backfill so two jobs never double the request
rate against public infrastructure, and it goes through run_enrich in bounded
batches so a kill costs at most one batch. run_enrich stamps award_probed_at on
a miss as well as a hit, so restarting never re-probes a tender.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

BATCH = 200


def _wait_for(pid: int, poll_s: int = 60) -> None:
    while os.path.exists(f"/proc/{pid}"):
        time.sleep(poll_s)


def main() -> int:
    wait_pid = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    from tenders.config import setup_logging

    setup_logging(logging.INFO)
    log = logging.getLogger("award-sweep")

    if wait_pid:
        log.info("waiting for in-flight backfill pid=%s", wait_pid)
        _wait_for(wait_pid)
        log.info("portal is free; starting award sweep")

    try:
        from tenders.enrich_awards import run_enrich
    except Exception as exc:  # noqa: BLE001
        log.error("enrich_awards did not import cleanly, refusing to run: %s", exc)
        return 1

    totals = {"probed": 0, "awards": 0, "batches": 0}
    while True:
        res = run_enrich(limit=BATCH, progress=lambda m: log.info("%s", m))
        probed = res.get("probed", 0)
        if not probed:
            log.info("queue empty; sweep complete")
            break
        totals["probed"] += probed
        totals["awards"] += res.get("awards", 0)
        totals["batches"] += 1
        log.info("batch %d: probed=%s awards=%s | running totals %s",
                 totals["batches"], probed, res.get("awards"), json.dumps(totals))

    log.info("award sweep finished: %s", json.dumps(totals))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
