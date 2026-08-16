"""Polite, stateful HTTP client for the GePNIC portal.

Wraps a single ``requests.Session`` with:
* cookie/session persistence (JSESSIONID) and a realistic User-Agent,
* a minimum-interval rate limiter with jitter (politeness by construction),
* bounded retries with exponential backoff on transient failures,
* a per-run request cap kill-switch.

Randomness for jitter uses the stdlib ``random`` module; this is fine for
politeness spacing (it never needs to be reproducible).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Config

log = logging.getLogger("http")


class RequestCapExceeded(RuntimeError):
    """Raised when the configured per-run request cap is hit (kill-switch)."""


@dataclass
class _RateLimiter:
    min_interval: float
    jitter: float
    _last: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        delay = self.min_interval + random.uniform(0, self.jitter)
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last = time.monotonic()


class HttpClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        s = cfg.scrape
        self.timeout = int(s["timeout_s"])
        self.max_retries = int(s["max_retries"])
        self.max_requests = int(s.get("max_requests_per_run", 0))
        self._count = 0
        self._lock = threading.Lock()
        self._limiter = _RateLimiter(float(s["min_interval_s"]), float(s["jitter_s"]))
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": s["user_agent"],
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._bootstrapped = False
        # Set once a document-download captcha has been solved this session; the
        # GePNIC server then serves every tender's document links without a
        # further captcha, so we only ever solve one per run.
        self.captcha_verified = False

    def bootstrap(self) -> None:
        """Establish a session cookie by hitting the app landing page once."""
        if self._bootstrapped:
            return
        try:
            self._raw_request("GET", self.cfg.host + "/nicgep/app?page=Home&service=page")
            self._bootstrapped = True
            log.debug("session bootstrapped; cookies=%s", self.session.cookies.get_dict())
        except Exception as exc:  # noqa: BLE001
            log.warning("bootstrap failed (continuing): %s", exc)

    def _check_cap(self) -> None:
        if self.max_requests and self._count >= self.max_requests:
            raise RequestCapExceeded(f"hit per-run request cap of {self.max_requests}")

    def _raw_request(self, method: str, url: str, **kwargs) -> requests.Response:
        @retry(
            retry=retry_if_exception_type(
                (requests.ConnectionError, requests.Timeout)
            ),
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=2, min=2, max=30),
            reraise=True,
        )
        def _do() -> requests.Response:
            with self._lock:
                self._check_cap()
                self._limiter.wait()
                self._count += 1
            resp = self.session.request(
                method, url, timeout=self.timeout, **kwargs
            )
            # Retry on transient server errors.
            if resp.status_code in (502, 503, 504):
                raise requests.ConnectionError(f"server {resp.status_code}")
            return resp

        return _do()

    def get(self, url: str, **kwargs) -> requests.Response:
        if not self._bootstrapped:
            self.bootstrap()
        return self._raw_request("GET", url, **kwargs)

    def post(self, url: str, data=None, **kwargs) -> requests.Response:
        if not self._bootstrapped:
            self.bootstrap()
        return self._raw_request("POST", url, data=data, **kwargs)

    @property
    def request_count(self) -> int:
        return self._count
