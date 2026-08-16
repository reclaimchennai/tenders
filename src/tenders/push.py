"""Web Push transport: VAPID identity, one send, and what a failure means.

Everything above this module deals in *what* to say; this module is the only
place that knows how to say it to a push service, and the only place that
decides a subscription is dead.

The private key never enters the repository, ``config.toml`` or a database
backup. ``[push].vapid_key_file`` in config.toml is a *path*; the file itself is
written 0600 inside a 0700 directory, and nothing here ever logs its contents.
Push endpoints get the same treatment for a different reason — an endpoint URL
identifies a person's device to their push provider and is a bearer credential
for pushing to it, so it is personal data twice over and is never written to a
log line. Failures are reported by subscription id and provider host, which is
all anyone debugging this needs.
"""

from __future__ import annotations

import base64
import functools
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("push")

# Chrome refuses a push whose payload exceeds 4 KB after encryption, and a
# notification body is a sentence. Anything longer is a bug in the caller.
MAX_PAYLOAD = 3500

# How long a push service should hold an undelivered message for a phone that is
# switched off. A day: a tender notification is worth reading tomorrow morning,
# and worth nothing next week.
TTL_S = 86400

# Consecutive hard failures before a subscription is abandoned. 404/410 delete
# immediately (see classify); this only bounds the "push service keeps 500ing"
# and "phone has been off for a month" cases, which are not evidence of death on
# any single attempt but are after ten of them.
MAX_FAILURES = 10


@dataclass(frozen=True)
class PushResult:
    """One delivery attempt. ``action`` is what the caller must do to the row."""

    ok: bool
    status: int | None
    action: str            # 'keep' | 'delete' | 'backoff' | 'fail'
    retry_after_s: int = 0
    detail: str = ""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_keys() -> tuple[str, str]:
    """A fresh VAPID P-256 keypair as (private PEM, public base64url).

    The public half is the ``applicationServerKey`` the browser pins into every
    subscription it creates, which is why rotating the private key is not a
    routine operation: every existing subscription is bound to the old public
    key and becomes undeliverable. Generate once, back the file up out of band.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return pem, _b64(raw)


def public_key_from_pem(pem: str) -> str:
    """The base64url ``applicationServerKey`` derived from a private key PEM."""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    return _b64(key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    ))


@dataclass(frozen=True)
class Vapid:
    """The signing identity, loaded once per process."""

    private_pem: str
    public_key: str
    subject: str


def key_path(cfg) -> Path:
    from .config import PROJECT_ROOT

    raw = (cfg.raw.get("push") or {}).get("vapid_key_file") \
        or "data/secrets/vapid_private.pem"
    p = Path(raw)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def load_vapid(cfg) -> Vapid | None:
    """The configured identity, or None when push has not been set up.

    Returning None rather than raising is deliberate: a mirror with no VAPID key
    must still serve the archive. Every push-facing endpoint degrades to "not
    available here" instead of a 500.
    """
    path = key_path(cfg)
    try:
        pem = path.read_text()
    except OSError:
        return None
    subject = (cfg.raw.get("push") or {}).get("subject") \
        or "mailto:admin@localhost"
    try:
        return Vapid(pem, public_key_from_pem(pem), subject)
    except Exception as exc:  # noqa: BLE001 - a corrupt key must not 500 the site
        log.error("VAPID key at %s is unusable: %s", path, type(exc).__name__)
        return None


def write_key(cfg, *, force: bool = False) -> tuple[Path, str]:
    """Create the keypair file if absent. Returns (path, public key)."""
    path = key_path(cfg)
    if path.exists() and not force:
        return path, public_key_from_pem(path.read_text())
    pem, pub = generate_keys()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Written through a private-by-construction handle: creating the file 0644
    # and chmod-ing afterwards leaves a window in which the key is world
    # readable, and on this host that window is on a multi-user machine.
    import os

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(pem)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    return path, pub


def provider(endpoint: str) -> str:
    """The push service's host — the only part of an endpoint safe to log."""
    try:
        return urlsplit(endpoint).netloc or "?"
    except ValueError:
        return "?"


def classify(status: int, retry_after: str | None = None) -> PushResult:
    """Map a push service's HTTP status onto what to do with the subscription.

    404 and 410 are the standard, unambiguous "this subscription no longer
    exists" — the user uninstalled the app, cleared site data, or the push
    service expired it. Retrying is not merely useless, it is the failure mode
    that keeps a dead endpoint in the table forever, so it deletes on the first
    one. 429 is the service asking for room and is honoured for exactly as long
    as it asks. 413 and 400 are our bug, not the subscriber's, so the row
    survives while the failure is counted and logged.
    """
    if status in (404, 410):
        return PushResult(False, status, "delete", detail="subscription gone")
    if status == 429:
        try:
            wait = int(retry_after or 0)
        except ValueError:
            wait = 0
        return PushResult(False, status, "backoff",
                          retry_after_s=max(60, min(wait or 900, 86400)),
                          detail="rate limited")
    if 200 <= status < 300:
        return PushResult(True, status, "keep")
    return PushResult(False, status, "fail", detail=f"http {status}")


@functools.lru_cache(maxsize=4)
def _signer(private_pem: str):
    """A py_vapid signer built from our PEM, cached for the process's life.

    pywebpush will happily take a key as a string, but ``Vapid.from_string``
    means *raw base64 DER*, not PEM — handing it a PEM raises an ASN.1 parse
    error from three layers down that reads like a corrupt key rather than a
    wrong format. Building the signer here also means the key is parsed once
    instead of on every notification.
    """
    from py_vapid import Vapid as PyVapid

    return PyVapid.from_pem(private_pem.encode("ascii"))


def send(vapid: Vapid, subscription: dict, payload: dict,
         *, timeout: float = 10.0, urgency: str = "normal",
         topic: str | None = None) -> PushResult:
    """Deliver one encrypted notification. Never raises.

    ``pywebpush`` is imported here rather than at module scope because it pulls
    in aiohttp and the encryption stack, and the web process — which imports
    this module for the public key alone on every start-up — should not pay for
    a dependency it uses only when someone presses "send me a test".
    """
    body = json.dumps(payload, separators=(",", ":"))
    if len(body.encode("utf-8")) > MAX_PAYLOAD:
        return PushResult(False, None, "fail", detail="payload too large")

    from pywebpush import WebPushException, webpush

    # Urgency and Topic go in as plain headers: they are protocol headers
    # (RFC 8030 §5.3, §5.4) that the push service reads, and pywebpush has no
    # named argument for either.
    headers = {"Urgency": urgency}
    if topic:
        # A topic collapses undelivered messages: a phone that was off for two
        # cycles wakes to the newest state of a watch, not a stack of superseded
        # ones. Push services require it to be short base64url text.
        headers["Topic"] = topic[:32]
    try:
        resp = webpush(
            subscription_info=subscription,
            data=body,
            vapid_private_key=_signer(vapid.private_pem),
            vapid_claims={"sub": vapid.subject},
            ttl=TTL_S,
            timeout=timeout,
            headers=headers,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status is None:
            return PushResult(False, None, "fail", detail=type(exc).__name__)
        retry_after = None
        try:
            retry_after = exc.response.headers.get("Retry-After")
        except Exception:  # noqa: BLE001
            pass
        return classify(status, retry_after)
    except Exception as exc:  # noqa: BLE001 - DNS, TLS, timeouts, all transient
        return PushResult(False, None, "fail", detail=type(exc).__name__)
    return classify(getattr(resp, "status_code", 201),
                    getattr(resp, "headers", {}).get("Retry-After")
                    if hasattr(resp, "headers") else None)
