"""One-off AOC (Award of Contract) backfill over the ransomware outage gap.

AOC is the largest bucket the portal's Tender Status listing exposes (~217k
records portal-wide) and the most useful for corruption work, because it names
who actually won each contract. This runner exists rather than a plain CLI call
because the job is long enough to need three things a bare invocation lacks:

* it waits for any in-flight status backfill to exit first — each HttpClient
  rate-limits itself, so two concurrent backfills would double the request rate
  against public government infrastructure;
* it refuses to start if the enumerator does not import cleanly, since other
  agents may be mid-edit on that module;
* it runs month-windowed and resumable, so a kill at any point costs only the
  current window.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

AOC = "6"


def _wait_for(pid: int, poll_s: int = 30) -> None:
    """Block until `pid` exits. Cheap: /proc check, no signals sent."""
    while os.path.exists(f"/proc/{pid}"):
        time.sleep(poll_s)


def main() -> int:
    start, end = sys.argv[1], sys.argv[2]
    wait_pid = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    from tenders.config import setup_logging

    setup_logging(logging.INFO)
    log = logging.getLogger("aoc")

    if wait_pid:
        log.info("waiting for in-flight backfill pid=%s to finish", wait_pid)
        _wait_for(wait_pid)
        log.info("portal is free; starting AOC")

    # Imported late and guarded: another agent owns this module and may be
    # part-way through an edit when this unit fires.
    try:
        from tenders.enumerate_status import run_status
    except Exception as exc:  # noqa: BLE001
        log.error("enumerate_status did not import cleanly, refusing to run: %s", exc)
        return 1

    res = run_status(statuses=[AOC], start=start, end=end,
                     progress=lambda m: log.info("%s", m))
    log.info("AOC backfill finished: %s", json.dumps(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
