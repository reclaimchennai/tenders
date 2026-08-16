"""The progressive retry schedule: 1m, 5m, 15m, 30m, then hourly.

Both document-missing retries (doc_lifecycle itself) and tender-level capture
retries (capture_retry.py) are driven by this one function, so a regression
here silently breaks the schedule the user actually asked for: "first attempt
in 1 minute, second in 5 minutes, third in 15 minutes, fourth in 30 minutes,
and after that every 60 minutes until we have all the documents."
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tenders.doc_lifecycle import (
    MAX_HOURLY_ATTEMPTS,
    RETRY_INTERVAL_S,
    SLOW_RETRY_INTERVAL_S,
    next_attempt_after,
)

NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def _minutes_after(attempts: int) -> float:
    due = datetime.fromisoformat(next_attempt_after(attempts, now=NOW))
    return (due - NOW).total_seconds() / 60


def test_first_four_retries_follow_the_requested_schedule():
    assert _minutes_after(1) == 1
    assert _minutes_after(2) == 5
    assert _minutes_after(3) == 15
    assert _minutes_after(4) == 30


def test_fifth_retry_onward_settles_to_hourly():
    assert _minutes_after(5) == RETRY_INTERVAL_S / 60 == 60
    assert _minutes_after(10) == 60
    assert _minutes_after(MAX_HOURLY_ATTEMPTS - 1) == 60


def test_past_the_hourly_ceiling_drops_to_a_daily_probe():
    due = datetime.fromisoformat(next_attempt_after(MAX_HOURLY_ATTEMPTS, now=NOW))
    assert (due - NOW).total_seconds() == SLOW_RETRY_INTERVAL_S
