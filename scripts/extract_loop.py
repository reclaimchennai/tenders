"""Continuous text extraction, decoupled from the scrape cycle.

``forward_capture.run_forward()`` calls ``run_extract`` only *after* its detail
loop finishes, which was fine when that loop was a few hundred tenders. It is
now tens of thousands: the cycle that began 2026-08-10 04:00 queued 80,813, and
at the portal's politeness spacing that is days before extraction gets a single
turn. Meanwhile every captured document stays invisible to search — which is the
one thing the archive exists to provide.

Extraction is pure local CPU (pdfplumber, tesseract, openpyxl) and touches the
portal not at all, so sequencing it behind a rate-limited network loop buys
nothing. This runs it independently instead, in bounded slices so a crash costs
one slice, and niced so OCR yields to the scraper that still has to meet its
delays.
"""

from __future__ import annotations

import json
import logging
import os
import time

# Long enough to amortise process start, short enough that a kill is cheap.
SLICE_SECONDS = 900
WORKERS = 4
IDLE_SLEEP = 300

# Floor on how often the loop may come back round. SLICE_SECONDS is a *ceiling*
# on a slice, not a floor: run_extract returns as soon as the queue is empty, so
# with the scraper trickling in 1-3 new documents a slice finished in ~8s and
# this loop spun continuously. Combined with the full tenders_fts rebuild that
# rebuild_fts used to do unconditionally, that was ~170,000 rows reindexed every
# 14 seconds, which took the public site down on 2026-08-15 (see index_fts's
# module docstring for the full account). Both halves are fixed — indexing is
# incremental now — but a busy-loop against the archive's single write lock is
# worth preventing on its own, so a slice that returns early is paced here.
MIN_CYCLE_SECONDS = 60


def main() -> int:
    from tenders.config import setup_logging

    setup_logging(logging.INFO)
    log = logging.getLogger("extract-loop")
    os.nice(10)

    from tenders.extract_text import run_extract
    from tenders.index_fts import rebuild_fts

    while True:
        started = time.monotonic()
        try:
            res = run_extract(max_seconds=SLICE_SECONDS, workers=WORKERS)
        except Exception as exc:  # noqa: BLE001 - a bad document must not end the loop
            log.warning("extract slice failed: %s", exc)
            time.sleep(60)
            continue

        done = res.get("processed", 0)
        log.info("slice: %s", json.dumps(res))

        # Index after every slice, not only the ones that extracted something.
        # rebuild_fts covers *tenders* as well as documents, and gating it on
        # `if done:` meant a tender the scraper had just discovered stayed
        # unsearchable until some unrelated document happened to need
        # extracting — on a quiet queue, potentially for hours. Both sides are
        # incremental now, so a pass with nothing to do writes nothing, which
        # is what makes running it unconditionally the cheap option.
        try:
            log.info("index: %s", json.dumps(rebuild_fts()))
        except Exception as exc:  # noqa: BLE001
            log.warning("index rebuild failed: %s", exc)

        if not done:
            log.info("nothing to extract; sleeping %ds", IDLE_SLEEP)
            time.sleep(IDLE_SLEEP)
            continue

        # Pace the loop even when there is work: see MIN_CYCLE_SECONDS.
        spent = time.monotonic() - started
        if spent < MIN_CYCLE_SECONDS:
            time.sleep(MIN_CYCLE_SECONDS - spent)


if __name__ == "__main__":
    raise SystemExit(main())
