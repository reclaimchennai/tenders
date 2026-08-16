"""The watchdog's two jobs: notice a wedge, and refuse to overreact to noise.

Everything asserted here is a property that failed for real on 2026-08-15 or
that would have made the fix worse than the fault:

* A 200 with an empty body is the wedge. The port accepted connections and
  systemd reported "active (running)" for the whole outage, so any check that
  stops at the status line reports a dead site as healthy.
* One slow probe is not an outage. The scraper shares this disk and makes pages
  slow; restarting on that would drop every in-flight request from a site that
  was merely busy, turning a hiccup into a self-inflicted outage.
* Restarting cannot be unbounded. The real fault on 15 August was in another
  unit entirely, so no number of web restarts would have helped — and bouncing
  a process that opens a 1.9GB database every two minutes makes a struggling
  box worse while hiding the actual bug.

None of these tests touch the network, systemd or the real database: the probe
and the clock are injected, so the logic is asserted directly rather than
inferred from a live service that happens to be up while the suite runs.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import healthcheck  # noqa: E402

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)


def _result(ok: bool, *, status=200, elapsed=0.01, body_bytes=288, detail="") -> healthcheck.ProbeResult:
    return healthcheck.ProbeResult(ok=ok, status=status, elapsed_s=elapsed,
                                   body_bytes=body_bytes, detail=detail)


# ---------------------------------------------------------------------------
# What counts as a real response


def test_two_hundred_with_an_empty_body_is_the_wedge_and_must_fail():
    """The exact signature of the incident: it answered, and served nothing."""
    ok, detail = healthcheck.validate_response(200, b"")
    assert not ok
    assert "0 bytes" in detail


def test_two_hundred_with_a_truncated_body_fails():
    ok, _ = healthcheck.validate_response(200, b"{")
    assert not ok


def test_a_real_healthz_payload_passes():
    body = json.dumps({"tenders_total": 95797, "documents_total": 178117}).encode()
    ok, detail = healthcheck.validate_response(200, body)
    assert ok
    assert "2 fields" in detail


def test_json_that_does_not_parse_fails_even_when_it_is_long_enough():
    """A half-flushed buffer is long but meaningless; length alone is not proof."""
    ok, detail = healthcheck.validate_response(200, b'{"tenders_total": 957')
    assert not ok
    assert "not valid JSON" in detail


def test_an_empty_json_object_carries_no_evidence_and_fails():
    ok, _ = healthcheck.validate_response(200, b'{                        }')
    assert not ok


@pytest.mark.parametrize("status", [301, 404, 500, 502, 503])
def test_any_non_200_fails(status):
    ok, _ = healthcheck.validate_response(status, b'{"tenders_total": 1}')
    assert not ok


def test_a_non_json_page_passes_on_length_alone():
    """Probing a plain HTML route instead of /healthz must still work."""
    ok, _ = healthcheck.validate_response(200, b"<!doctype html><title>x</title>")
    assert ok


# ---------------------------------------------------------------------------
# Consecutive failures, not one


def test_a_single_failure_followed_by_success_reports_healthy():
    """The anti-overreaction rule. A slow page during a scrape burst is not an
    outage, and a restart fired on noise is worse than the noise."""
    outcomes = [_result(False), _result(True)]
    seen: list[float] = []
    results = healthcheck.check_health(
        "http://x", probe=lambda url: outcomes.pop(0),
        sleep=seen.append)
    assert results[-1].ok
    assert len(results) == 2
    assert seen == [healthcheck.FAILURE_SPACING_S]


def test_probing_stops_at_the_first_success():
    calls = {"n": 0}

    def probe(url):
        calls["n"] += 1
        return _result(True)

    healthcheck.check_health("http://x", probe=probe, sleep=lambda s: None)
    assert calls["n"] == 1


def test_all_probes_failing_reports_unhealthy_with_every_attempt_kept():
    """The evidence is the point: three silent probes at 10s each is a
    diagnosis, 'unhealthy' on its own is a rumour."""
    results = healthcheck.check_health(
        "http://x",
        probe=lambda url: _result(False, status=None, elapsed=10.0, body_bytes=0,
                                  detail="no complete response"),
        sleep=lambda s: None)
    assert len(results) == healthcheck.CONSECUTIVE_FAILURES
    assert not any(r.ok for r in results)
    assert "elapsed=10.00s" in str(results[0])


def test_the_default_requires_more_than_one_failure():
    assert healthcheck.CONSECUTIVE_FAILURES >= 2


# ---------------------------------------------------------------------------
# Restart-storm guard


def test_restarts_outside_the_window_are_forgotten():
    stamps = [
        (NOW - timedelta(minutes=90)).isoformat(),   # older than the window
        (NOW - timedelta(minutes=61)).isoformat(),   # just outside
        (NOW - timedelta(minutes=59)).isoformat(),   # just inside
        (NOW - timedelta(minutes=1)).isoformat(),
    ]
    kept = healthcheck.prune_restarts(stamps, NOW)
    assert len(kept) == 2


def test_unparseable_ledger_entries_are_dropped_rather_than_raising():
    """A corrupt ledger must cost storm protection at worst, never the check."""
    kept = healthcheck.prune_restarts(["not-a-timestamp", NOW.isoformat()], NOW)
    assert kept == [NOW.isoformat()]


def test_naive_timestamps_are_read_as_utc_not_rejected():
    naive = (NOW - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    assert healthcheck.prune_restarts([naive], NOW) == [naive]


def test_the_guard_trips_at_the_limit_and_not_before():
    stamps = [NOW.isoformat()] * (healthcheck.MAX_RESTARTS_PER_HOUR - 1)
    assert not healthcheck.storm_guard_tripped(stamps)
    stamps.append(NOW.isoformat())
    assert healthcheck.storm_guard_tripped(stamps)


def test_the_ledger_round_trips_through_a_file(tmp_path):
    path = tmp_path / "state" / "healthcheck_state.json"
    stamps = [NOW.isoformat(), (NOW - timedelta(minutes=10)).isoformat()]
    healthcheck.save_restarts(path, stamps)
    assert healthcheck.load_restarts(path) == stamps


def test_a_missing_or_corrupt_ledger_reads_as_empty(tmp_path):
    assert healthcheck.load_restarts(tmp_path / "nope.json") == []
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    assert healthcheck.load_restarts(corrupt) == []


def test_the_state_file_lives_beside_the_database_by_default(monkeypatch):
    """It must survive a reboot: a guard that forgets its history at boot would
    happily restart forever across a crash loop."""
    monkeypatch.delenv("TENDERS_HEALTHCHECK_STATE", raising=False)
    path = healthcheck.state_path()
    assert path.name == "healthcheck_state.json"
    assert path.parent.name == "data"


# ---------------------------------------------------------------------------
# Which URL gets probed


def test_the_default_port_is_the_one_the_unit_actually_binds(monkeypatch):
    """config.toml says [web] port = 8000 but tenders-web.service passes
    --port 8013, and cli.py:web_cmd lets the command line win. A watchdog that
    trusted the config would probe a dead port, find nothing, and restart a
    healthy site every two minutes forever."""
    for var in ("TENDERS_HEALTHCHECK_URL", "TENDERS_WEB_PORT",
                "TENDERS_WEB_HOST", "TENDERS_HEALTHCHECK_PATH"):
        monkeypatch.delenv(var, raising=False)
    assert healthcheck.probe_url() == "http://127.0.0.1:8013/healthz"
    assert healthcheck.DEFAULT_PORT == 8013


def test_the_port_env_var_overrides_the_default(monkeypatch):
    monkeypatch.delenv("TENDERS_HEALTHCHECK_URL", raising=False)
    monkeypatch.setenv("TENDERS_WEB_PORT", "9999")
    assert healthcheck.probe_url() == "http://127.0.0.1:9999/healthz"


def test_a_full_url_override_wins_over_everything(monkeypatch):
    monkeypatch.setenv("TENDERS_WEB_PORT", "9999")
    monkeypatch.setenv("TENDERS_HEALTHCHECK_URL", "http://elsewhere:1/ping")
    assert healthcheck.probe_url() == "http://elsewhere:1/ping"


# ---------------------------------------------------------------------------
# Recovery verification


def test_recovery_polling_gives_up_at_the_deadline_instead_of_hanging():
    clock = {"t": 0.0}

    def monotonic():
        return clock["t"]

    def sleep(s):
        clock["t"] += s

    assert healthcheck.await_recovery(
        "http://x", timeout_s=30.0, poll_s=3.0,
        probe=lambda url: _result(False), sleep=sleep, monotonic=monotonic) is None
    assert clock["t"] >= 30.0


def test_recovery_returns_the_probe_that_proved_the_site_is_back():
    outcomes = [_result(False), _result(False), _result(True, elapsed=0.42)]
    got = healthcheck.await_recovery(
        "http://x", probe=lambda url: outcomes.pop(0), sleep=lambda s: None)
    assert got is not None and got.ok
    assert got.elapsed_s == 0.42


# ---------------------------------------------------------------------------
# The real probe, against a server we control


def test_probe_once_rejects_a_server_that_accepts_and_says_nothing():
    """End to end against a socket that behaves exactly as the wedged app did:
    it completes the TCP handshake and then never writes a byte. A connect-only
    check calls this healthy; this one must not, and must return within the
    deadline rather than hanging the watchdog."""
    import socket
    import threading

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept_and_stall():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            held.append(conn)  # accepted, never answered

    server = threading.Thread(target=accept_and_stall, daemon=True)
    server.start()
    try:
        result = healthcheck.probe_once(f"http://127.0.0.1:{port}/healthz",
                                        timeout_s=1.0)
    finally:
        stop.set()
        listener.close()
        for conn in held:
            conn.close()

    assert not result.ok
    assert result.elapsed_s < 5.0, "the probe must bound itself, not hang"


def test_probe_once_reports_a_closed_port_as_unhealthy():
    """A refused connection is a crash, which systemd's Restart= should already
    have handled. Reported unhealthy anyway: if the port is shut for three
    consecutive probes, then it did not."""
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    result = healthcheck.probe_once(f"http://127.0.0.1:{port}/healthz", timeout_s=2.0)
    assert not result.ok
    assert result.status is None
