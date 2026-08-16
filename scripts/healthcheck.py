"""Watchdog for the public site: detect a *wedge*, not just a crash, and heal it.

On 2026-08-15 ``tenders-web`` stopped serving without ever dying. The uvicorn
process stayed alive, ``systemctl --user status`` said "active (running)", and
the listening socket on port 8013 kept completing TCP handshakes — it simply
never wrote a single byte of any response again. A runaway ``tenders-extract``
loop was rebuilding the whole 170,000-row search index every ~14 seconds, which
pinned the disk, held the SQLite write lock continuously and grew the WAL to
44 GB against a 1.9 GB database; the web process starved behind it. The site was
down for an unknown length of time and was noticed by a human, not by the box.

Every automatic defence already installed missed it, and each one missed it for
the same reason — *the process never exited*:

* ``Restart=always`` in tenders-web.service only fires on exit. Nothing exited.
* systemd's own liveness notion for ``Type=simple`` is "did the main PID go
  away", which is the one question whose answer was still "no".
* A TCP-connect probe would have reported the site healthy for the entire
  outage, because ``accept()`` lives in the kernel and keeps working long after
  the application behind it has stopped running. The listen backlog was fine.
  The application was not.

So this probe deliberately does the one thing those checks do not: it requires a
**complete, valid HTTP response body** to come back inside a hard wall-clock
deadline. Bytes on the wire are the only evidence that the event loop is still
turning. Anything less and the outage repeats.

Two failure modes are guarded against just as carefully as the wedge itself,
because a watchdog that overreacts is worse than no watchdog:

* **Restarting on noise.** The scraper shares this disk and legitimately makes a
  page slow now and then. One slow response is not an outage, so a restart needs
  ``CONSECUTIVE_FAILURES`` independent failures spaced ``FAILURE_SPACING_S``
  apart. Dropping every in-flight request from a site that was merely busy is a
  self-inflicted outage.
* **Restart storms.** If restarting does not help — because the real fault is
  elsewhere, as it was on 15 August, when the cause was another unit entirely —
  then restarting forever hides the bug, and bouncing a process that is loading
  a 1.9 GB database every two minutes makes the box *worse*. After
  ``MAX_RESTARTS_PER_HOUR`` the watchdog stops acting and starts shouting.

Exit status is the reporting channel, so that ``systemctl --user list-units
--failed`` and the journal tell the story without anyone reading this file:
0 means healthy and untouched; every non-zero code means the watchdog had to
act, and which code says what happened next.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

UNIT = "tenders-web.service"

# The port the unit actually binds, which is *not* the port in config.toml.
#
# config.toml carries `[web] port = 8000`, but deploy/tenders-web.service starts
# the app as `tenders-web --host 0.0.0.0 --port 8013`, and cli.py:web_cmd
# resolves it as `port = args.port or int(cfg.web["port"])` — the command line
# wins, so the config value has been dead for the whole life of this deployment.
# A watchdog that "read the port from config" as one might reasonably expect
# would therefore probe 8000, find nothing listening, conclude the site is down,
# and restart a perfectly healthy service every two minutes forever. That is
# precisely the self-inflicted outage this file exists to avoid, so the default
# here is the port that is really bound, TENDERS_WEB_PORT overrides it, and the
# config value is only ever *read to be complained about* (see _config_port).
DEFAULT_PORT = 8013
DEFAULT_HOST = "127.0.0.1"

# /healthz is the cheapest honest endpoint on the site: 287 bytes, ~2 ms, and
# its payload is memoised for STATS_TTL=60s, so probing it every two minutes
# costs at most one real aggregate query per probe and usually none. It is still
# a true liveness signal — rendering it requires the event loop to accept the
# connection, run the handler and flush the response, which is exactly the chain
# that was broken during the wedge. Its body is also *checkable* (see
# validate_response), unlike "/" whose HTML would need parsing to distinguish a
# real page from a truncated one.
DEFAULT_PATH = "/healthz"

# Hard wall-clock ceiling for one probe. Generous on purpose: the front page
# takes ~0.5 s when the scraper is mid-cycle, and the point of the deadline is
# to catch "never answers", not "answered slowly".
TIMEOUT_S = 10.0

# Three strikes, five seconds apart: ~40 s of sustained silence before anything
# is touched. Long enough that a single blocked query cannot cause a restart,
# short enough that the timer's 2-minute cadence still bounds an outage to a
# few minutes rather than the "unknown period" of the incident.
CONSECUTIVE_FAILURES = 3
FAILURE_SPACING_S = 5.0

# A response of 200 with a zero-length body is the exact signature of the wedge
# and must count as a failure, so "did we get bytes" is asserted separately from
# "did we get a status line". /healthz is 287 bytes; anything under this floor
# is a truncated or empty reply, not a page.
MIN_BODY_BYTES = 16
# Cap on what is read back. The probe must never be able to consume memory on a
# box that is already under pressure — that is how a watchdog joins the outage.
MAX_BODY_BYTES = 64 * 1024

MAX_RESTARTS_PER_HOUR = 3
RESTART_WINDOW = timedelta(hours=1)

# Bounded so a hung `systemctl` cannot hang the watchdog, but generous, because
# how long a restart takes depends on how the unit is stuck.
#
# Measured against a SIGSTOPped tenders-web on this box: 3 seconds. That is
# faster than it sounds like it should be — a stopped process cannot act on
# SIGTERM — and the reason is that systemd defaults to SendSIGCONT=yes, so it
# wakes the process up as it signals it. The bad case is the one this margin is
# for: a process that is running but stuck inside an uninterruptible syscall
# (the 2026-08-15 shape, blocked on a disk saturated by another unit) does
# ignore SIGTERM, and systemd then waits the whole TimeoutStopSec — 90s by
# default, which tenders-web.service does not override — before SIGKILL. A
# timeout under that would abandon the one restart that was about to work.
RESTART_TIMEOUT_S = 180.0

# After the restart, give uvicorn room to import the app and open the 1.9 GB
# database before deciding the cure failed.
RECOVERY_TIMEOUT_S = 60.0
RECOVERY_POLL_S = 3.0

# Exit codes. All non-zero values put the oneshot unit into "failed", which is
# the intended alarm: `systemctl --user list-units --failed` names the incident.
EXIT_HEALTHY = 0
EXIT_RESTARTED_RECOVERED = 1
EXIT_RESTARTED_STILL_DOWN = 2
EXIT_STORM_GUARD = 3
EXIT_RESTART_FAILED = 4

log = logging.getLogger("healthcheck")


@dataclass(frozen=True)
class ProbeResult:
    """One HTTP attempt, with enough detail to be worth reading in the journal.

    ``elapsed_s`` is recorded even for failures because the *shape* of the
    failure is the diagnosis: ~10 s means silence (a wedge), ~0.001 s means the
    connection was refused (a crash, which systemd's Restart= already handles),
    and anything in between is usually a real error page.
    """

    ok: bool
    status: int | None
    elapsed_s: float
    body_bytes: int
    detail: str

    def __str__(self) -> str:
        status = self.status if self.status is not None else "-"
        return (f"status={status} bytes={self.body_bytes} "
                f"elapsed={self.elapsed_s:.2f}s {self.detail}")


# ---------------------------------------------------------------------------
# Where to probe


def _config_port() -> int | None:
    """The port config.toml *claims*, purely so drift can be reported.

    Never used to build the probe URL — see DEFAULT_PORT for why that would be
    actively dangerous. Import failures are swallowed: this watchdog has to keep
    working while somebody is mid-edit inside src/tenders/, which is exactly the
    situation in which the site is most likely to break.
    """
    try:
        from tenders.config import load_config

        return int(load_config().web["port"])
    except Exception:  # noqa: BLE001 - a broken config must not blind the watchdog
        return None


def probe_url() -> str:
    """The URL to probe, most explicit source first.

    TENDERS_HEALTHCHECK_URL is the full override for anyone running the site
    somewhere unusual; TENDERS_WEB_PORT covers the ordinary "we moved the port"
    case and is the one thing that must be kept in step with the unit file.
    """
    override = os.environ.get("TENDERS_HEALTHCHECK_URL")
    if override:
        return override
    host = os.environ.get("TENDERS_WEB_HOST", DEFAULT_HOST)
    port = int(os.environ.get("TENDERS_WEB_PORT", DEFAULT_PORT))
    path = os.environ.get("TENDERS_HEALTHCHECK_PATH", DEFAULT_PATH)
    return f"http://{host}:{port}{path}"


# ---------------------------------------------------------------------------
# The probe itself


def validate_response(status: int, body: bytes) -> tuple[bool, str]:
    """Is this a real response, or the ghost of one?

    Split out from the I/O so the wedge signature can be asserted in tests
    without a socket. The empty-body case is the whole point of the file: during
    the incident the port answered and the application did not, and any check
    that stopped at the status line would have called that healthy.
    """
    if status != 200:
        return False, f"HTTP {status} is not 200"
    if len(body) < MIN_BODY_BYTES:
        return False, (f"body of {len(body)} bytes is below the {MIN_BODY_BYTES}"
                       " byte floor — answered but served nothing")
    # /healthz is JSON with corpus counts. Requiring a key to actually parse out
    # of it means a proxy error page or a half-flushed buffer cannot pass as a
    # healthy site just by being long enough.
    stripped = body.lstrip()
    if stripped.startswith(b"{"):
        try:
            payload = json.loads(body)
        except ValueError as exc:
            return False, f"body is not valid JSON: {exc}"
        if not isinstance(payload, dict) or not payload:
            return False, "JSON body carries no fields"
        return True, f"valid JSON, {len(payload)} fields, {len(body)} bytes"
    return True, f"{len(body)} bytes"


def _request(url: str, timeout_s: float, out: dict) -> None:
    """Perform one request, recording the outcome in ``out``.

    Runs on a worker thread (see probe_once) and must never raise out of it.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tenders-healthcheck/1"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read(MAX_BODY_BYTES)
            ok, detail = validate_response(resp.status, body)
            out["result"] = (ok, resp.status, len(body), detail)
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(MAX_BODY_BYTES)
        except Exception:  # noqa: BLE001
            pass
        out["result"] = (False, exc.code, len(body), f"HTTP error: {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        # Connection refused lands here, and is a *crash*, not a wedge. It is
        # still reported as unhealthy: systemd should have caught it, and if the
        # port is closed for three consecutive probes then systemd did not.
        out["result"] = (False, None, 0, f"{type(exc).__name__}: {exc}")


def probe_once(url: str, timeout_s: float = TIMEOUT_S) -> ProbeResult:
    """One probe with a *wall-clock* deadline, not merely a socket deadline.

    The request runs on a daemon thread that is joined with a timeout and then
    abandoned if it has not finished. That extra machinery buys a guarantee the
    plain ``urlopen(timeout=...)`` argument does not give: a socket timeout
    bounds each individual recv, so a server that dribbles one byte every nine
    seconds resets it forever and hangs the probe indefinitely. A wedge that
    hangs the *watchdog* is a watchdog that reports nothing at all, and the
    timer would then pile up runs against a unit that never exits. Joining with a
    deadline bounds the whole attempt no matter how the far end misbehaves; the
    orphaned thread is harmless because this process is about to exit anyway.
    """
    out: dict = {}
    started = time.monotonic()
    worker = threading.Thread(target=_request, args=(url, timeout_s, out), daemon=True)
    worker.start()
    worker.join(timeout_s)
    elapsed = time.monotonic() - started

    if worker.is_alive() or "result" not in out:
        return ProbeResult(
            ok=False, status=None, elapsed_s=elapsed, body_bytes=0,
            detail=(f"no complete response within {timeout_s:.0f}s — this is the"
                    " wedge signature (socket accepted, nothing served)"),
        )
    ok, status, body_bytes, detail = out["result"]
    return ProbeResult(ok=ok, status=status, elapsed_s=elapsed,
                       body_bytes=body_bytes, detail=detail)


def check_health(url: str, *, attempts: int = CONSECUTIVE_FAILURES,
                 spacing_s: float = FAILURE_SPACING_S,
                 probe=probe_once, sleep=time.sleep) -> list[ProbeResult]:
    """Probe until one attempt succeeds or ``attempts`` in a row have failed.

    Returns every result, because the evidence is what makes the journal line
    worth having: "three probes, all silent at 10.0 s" is a diagnosis, while
    "unhealthy" is a rumour. A single success short-circuits — the site answered,
    and nothing more needs to be proven.
    """
    results: list[ProbeResult] = []
    for attempt in range(1, attempts + 1):
        result = probe(url)
        results.append(result)
        if result.ok:
            log.info("probe %d/%d ok: %s", attempt, attempts, result)
            return results
        log.warning("probe %d/%d FAILED: %s", attempt, attempts, result)
        if attempt < attempts:
            sleep(spacing_s)
    return results


# ---------------------------------------------------------------------------
# Restart-storm guard


def state_path() -> Path:
    """Where the restart ledger lives.

    Next to the database, so it shares the archive's own backup and retention
    story and survives a reboot — a storm guard that forgets its history at boot
    would happily restart forever across a crash loop, which is the case it most
    needs to catch. Falls back to the project's data/ directory by path when the
    package cannot be imported, because the watchdog must outlive a broken
    checkout.
    """
    override = os.environ.get("TENDERS_HEALTHCHECK_STATE")
    if override:
        return Path(override)
    try:
        from tenders.config import load_config

        return load_config().db_path.parent / "healthcheck_state.json"
    except Exception:  # noqa: BLE001
        return PROJECT_ROOT / "data" / "healthcheck_state.json"


def load_restarts(path: Path) -> list[str]:
    """Timestamps of past restarts. A missing or corrupt ledger reads as empty.

    Never raises. If this file is unreadable the correct behaviour is to allow
    the restart and lose the storm protection for one run, not to refuse to
    check the site at all.
    """
    try:
        data = json.loads(path.read_text())
        stamps = data.get("restarts", [])
        return [s for s in stamps if isinstance(s, str)]
    except Exception:  # noqa: BLE001
        return []


def prune_restarts(stamps: list[str], now: datetime,
                   window: timedelta = RESTART_WINDOW) -> list[str]:
    """Drop restarts older than the window, and anything unparseable with them.

    The ledger is only ever consulted to answer "how many in the last hour", so
    old entries are not history worth keeping — and pruning on every write is
    what stops the file growing without bound on a box nobody is watching.
    """
    cutoff = now - window
    kept: list[str] = []
    for stamp in stamps:
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            kept.append(stamp)
    return kept


def save_restarts(path: Path, stamps: list[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"restarts": stamps}, indent=2))
    except OSError as exc:
        # Losing the ledger costs storm protection, not availability, so this is
        # a warning and the restart still goes ahead.
        log.warning("could not persist restart ledger at %s: %s", path, exc)


def storm_guard_tripped(stamps: list[str],
                        limit: int = MAX_RESTARTS_PER_HOUR) -> bool:
    """Have we already restarted enough times this hour to stop trying?

    Restarting is only a cure for a process that has gone bad on its own. When
    the fault is upstream — a disk saturated by another unit, a 44 GB WAL, a
    database that will not open — restarting changes nothing and costs the
    reload of a 1.9 GB archive each time. Past this point the honest action is to
    stop and make noise, so a human sees an alarm instead of a service that has
    been quietly flapping all night.
    """
    return len(stamps) >= limit


# ---------------------------------------------------------------------------
# Acting


def restart_unit(unit: str = UNIT, timeout_s: float = RESTART_TIMEOUT_S) -> tuple[bool, str]:
    """``systemctl --user restart``, bounded so it cannot hang the watchdog."""
    cmd = ["systemctl", "--user", "restart", unit]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False, f"`{' '.join(cmd)}` did not return within {timeout_s:.0f}s"
    except OSError as exc:
        return False, f"could not run systemctl: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, f"systemctl exited {proc.returncode}: {detail}"
    return True, "systemctl reported success"


def await_recovery(url: str, *, timeout_s: float = RECOVERY_TIMEOUT_S,
                   poll_s: float = RECOVERY_POLL_S,
                   probe=probe_once, sleep=time.sleep,
                   monotonic=time.monotonic) -> ProbeResult | None:
    """Poll until the site answers properly again, or the grace period expires.

    Restarting and walking away would leave the same "nobody knew" hole the
    incident exposed, one layer further in: the journal must record whether the
    cure worked, not merely that it was attempted.
    """
    deadline = monotonic() + timeout_s
    last: ProbeResult | None = None
    while True:
        last = probe(url)
        if last.ok:
            return last
        if monotonic() >= deadline:
            return None
        sleep(poll_s)


# ---------------------------------------------------------------------------


def main() -> int:
    try:
        from tenders.config import setup_logging

        setup_logging(logging.INFO)
    except Exception:  # noqa: BLE001 - never let a broken checkout mute the watchdog
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )

    url = probe_url()
    configured = _config_port()
    probed_port = int(os.environ.get("TENDERS_WEB_PORT", DEFAULT_PORT))
    if configured is not None and configured != probed_port:
        # Reported, never acted on. See DEFAULT_PORT: acting on config.toml here
        # would restart a healthy site every two minutes forever.
        log.info("note: config.toml [web].port=%s but the unit binds %s; probing"
                 " %s (the port that is actually listening)",
                 configured, probed_port, probed_port)

    results = check_health(url)
    if results and results[-1].ok:
        log.info("healthy: %s answered in %.3fs", url, results[-1].elapsed_s)
        return EXIT_HEALTHY

    evidence = "; ".join(f"[{i}] {r}" for i, r in enumerate(results, 1))
    log.error("UNHEALTHY: %d consecutive failed probes of %s — %s",
              len(results), url, evidence)

    path = state_path()
    now = datetime.now(timezone.utc)
    stamps = prune_restarts(load_restarts(path), now)

    if storm_guard_tripped(stamps):
        log.critical(
            "EMERGENCY: %s is not serving and has already been restarted %d times"
            " in the last hour (%s). NOT restarting again — restarting is not"
            " fixing this and the real fault is elsewhere (on 2026-08-15 it was"
            " tenders-extract saturating the disk, and no number of web restarts"
            " would have helped). A human needs to look at this box now.",
            UNIT, len(stamps), ", ".join(stamps))
        return EXIT_STORM_GUARD

    log.error("restarting %s (restarts in the last hour: %d of %d allowed)",
              UNIT, len(stamps), MAX_RESTARTS_PER_HOUR)
    # The ledger is written *before* the restart, deliberately. A restart that
    # kills this process, or a box that reboots mid-command, must still count
    # against the budget — an unrecorded attempt is how a storm guard is talked
    # out of guarding.
    stamps.append(now.isoformat())
    save_restarts(path, stamps)

    ok, detail = restart_unit()
    if not ok:
        log.critical("EMERGENCY: restart of %s FAILED: %s. The site is down and"
                     " the watchdog could not act.", UNIT, detail)
        return EXIT_RESTART_FAILED
    log.error("restart issued: %s", detail)

    recovered = await_recovery(url)
    if recovered is not None:
        log.error("RECOVERED: %s is serving again (%s). The wedge is healed;"
                  " this unit still exits non-zero so the incident is visible in"
                  " `systemctl --user list-units --failed`.", url, recovered)
        return EXIT_RESTARTED_RECOVERED

    log.critical("EMERGENCY: %s was restarted but %s still did not serve a valid"
                 " response within %.0fs. The site is DOWN.",
                 UNIT, url, RECOVERY_TIMEOUT_S)
    return EXIT_RESTARTED_STILL_DOWN


if __name__ == "__main__":
    raise SystemExit(main())
