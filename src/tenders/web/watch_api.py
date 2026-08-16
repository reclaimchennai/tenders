"""HTTP surface for push subscriptions, watches and bookmark alerts.

Two things here are deliberate and load-bearing.

**The push endpoint is the credential, so it is never in a URL.** Every route
that names a subscription takes it in a POST body, including the read-only ones
that would otherwise be natural GETs. A subscription endpoint contains a long
unguessable token issued by the browser's push service, which makes it a
perfectly good anonymous capability — nobody but that browser knows it — but it
is also personal data, and access logs, Referer headers and Cloudflare all see
query strings. A GET would leak every subscriber's endpoint into three logs we
do not control.

**Writes are separated from reads.** The rest of the app is read-only against
the archive (see web.app) and must stay that way; these routes are the only
writers in the web process. They get their own connection with the same 30s
busy_timeout the scraper uses, because the scraper holds write locks for seconds
at a time and a subscribe button must wait rather than fail.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import load_config
from ..db import connect
from .. import push as push_mod
from ..watches import (
    add_alert,
    add_watch,
    canonical_filters,
    describe,
    forget,
    subscription_id,
    upsert_subscription,
)

log = logging.getLogger("watch_api")
router = APIRouter()
cfg = load_config()

# Loaded once. A mirror deployed without a key still serves the archive; the
# routes below answer "not configured" and the UI hides the control.
_vapid = push_mod.load_vapid(cfg)

_local = threading.local()


def _writer():
    """One write connection per request thread (FastAPI runs these in a pool).

    Autocommit (``isolation_level=None``), and that is the whole fix for
    "database is locked" on subscribing.

    Python's sqlite3 defaults to *deferred* transactions: it silently opens one
    on the first INSERT, having possibly already read on the same connection.
    A deferred transaction that has read and then wants to write must upgrade
    its lock, and if any other process has written in between, SQLite cannot
    grant the upgrade without breaking this connection's snapshot — so it
    returns SQLITE_BUSY **immediately**. ``busy_timeout`` does not apply,
    because waiting cannot resolve it. That is exactly what a subscriber saw:
    an instant HTTP 500 despite a 30-second timeout, on a database being
    written continuously by the scraper, the extractor and the award sweep.

    In autocommit each statement takes the write lock up front, which is a
    conflict ``busy_timeout`` *can* wait out. Multi-statement writes that need
    to be atomic take an explicit ``BEGIN IMMEDIATE`` instead (see
    ``_immediate``) — same reasoning, one lock, acquired first.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect(cfg.db_path)
        conn.isolation_level = None
        _local.conn = conn
    # A previous request on this thread may have died mid-transaction; a stale
    # open transaction here would reintroduce the upgrade deadlock for every
    # later request on the same worker thread.
    if conn.in_transaction:
        conn.rollback()
    return conn


@contextmanager
def _immediate(conn):
    """Run a multi-statement write under one up-front write lock."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


# Bounds on anything a browser can put in the database. These are not security
# boundaries — the endpoint is unguessable, so there is no attacker to bound —
# they stop a buggy client turning a subscription row into a megabyte.
MAX_ENDPOINT = 2000
MAX_FILTERS = 4000
MAX_LABEL = 120
MAX_WATCHES_PER_SUB = 50
MAX_ALERTS_PER_SUB = 500


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "expected a JSON body")
    if not isinstance(data, dict):
        raise HTTPException(400, "expected a JSON object")
    return data


def _endpoint(data: dict) -> str:
    sub = data.get("subscription") or {}
    endpoint = (data.get("endpoint") or sub.get("endpoint") or "").strip()
    if not endpoint.startswith("https://") or len(endpoint) > MAX_ENDPOINT:
        raise HTTPException(400, "invalid push endpoint")
    return endpoint


def _require_sub(conn, endpoint: str) -> int:
    sub_id = subscription_id(conn, endpoint)
    if sub_id is None:
        raise HTTPException(404, "unknown subscription")
    return sub_id


@router.get("/api/push/key")
def push_key():
    """The applicationServerKey the browser needs to create a subscription."""
    if _vapid is None:
        return JSONResponse({"enabled": False}, status_code=503)
    return {"enabled": True, "key": _vapid.public_key}


@router.post("/api/push/register")
async def register(request: Request):
    """Record a browser's subscription without saving anything to it yet.

    Called on every load of a page that already has permission, which is what
    keeps ``last_seen_at`` meaningful and re-attaches a subscription the push
    service silently rotated.
    """
    data = await _body(request)
    sub = data.get("subscription") or {}
    keys = sub.get("keys") or {}
    endpoint = _endpoint(data)
    p256dh, auth = (keys.get("p256dh") or "").strip(), (keys.get("auth") or "").strip()
    if not p256dh or not auth:
        raise HTTPException(400, "subscription is missing its keys")
    platform = (data.get("platform") or "")[:16] or None
    conn = _writer()
    sub_id = upsert_subscription(conn, endpoint, p256dh, auth, platform)
    replaces = (data.get("replaces") or "").strip()
    if replaces and replaces != endpoint:
        # pushsubscriptionchange: the push service rotated this browser's
        # endpoint. Without this the old row survives to 410 forever, and the
        # watches hanging off it are silently orphaned rather than moved.
        old = subscription_id(conn, replaces)
        if old is not None:
            # One transaction: moving the watches and deleting the old row must
            # not be separable, or a failure between them orphans them.
            with _immediate(conn):
                conn.execute("UPDATE OR IGNORE watches SET subscription_id=?"
                             " WHERE subscription_id=?", (sub_id, old))
                conn.execute("UPDATE OR IGNORE tender_alerts SET subscription_id=?"
                             " WHERE subscription_id=?", (sub_id, old))
                conn.execute("DELETE FROM push_subscriptions WHERE id=?", (old,))
    return {"ok": True, "watches": _watch_rows(conn, sub_id),
            "alerts": _alert_rows(conn, sub_id)}


def _watch_rows(conn, sub_id: int) -> list[dict]:
    return [{"id": r["id"], "filters": r["filters"], "label": r["label"],
             "created_at": r["created_at"], "last_notified_at": r["last_notified_at"],
             "notified_count": r["notified_count"]}
            for r in conn.execute(
                "SELECT * FROM watches WHERE subscription_id=? AND active=1"
                " ORDER BY id DESC", (sub_id,))]


def _alert_rows(conn, sub_id: int) -> list[dict]:
    return [{"tender_id": r["tender_id"], "registered_at": r["registered_at"],
             "last_notified_at": r["last_notified_at"],
             "notified_count": r["notified_count"]}
            for r in conn.execute(
                "SELECT * FROM tender_alerts WHERE subscription_id=? AND active=1"
                " ORDER BY id DESC", (sub_id,))]


@router.post("/api/watch/list")
async def watch_list(request: Request):
    conn = _writer()
    sub_id = _require_sub(conn, _endpoint(await _body(request)))
    return {"watches": _watch_rows(conn, sub_id), "alerts": _alert_rows(conn, sub_id)}


@router.post("/api/watch/subscribe")
async def watch_subscribe(request: Request):
    """Save the search currently on screen as a watch."""
    data = await _body(request)
    endpoint = _endpoint(data)
    filters = (data.get("filters") or "")[:MAX_FILTERS]
    sub = data.get("subscription") or {}
    keys = sub.get("keys") or {}
    conn = _writer()
    if keys.get("p256dh") and keys.get("auth"):
        sub_id = upsert_subscription(conn, endpoint, keys["p256dh"], keys["auth"],
                                     (data.get("platform") or "")[:16] or None)
    else:
        sub_id = _require_sub(conn, endpoint)
    count = conn.execute(
        "SELECT count(*) FROM watches WHERE subscription_id=? AND active=1",
        (sub_id,)).fetchone()[0]
    canon = canonical_filters(filters)
    existing = conn.execute(
        "SELECT 1 FROM watches WHERE subscription_id=? AND filters=?",
        (sub_id, canon)).fetchone()
    if count >= MAX_WATCHES_PER_SUB and not existing:
        raise HTTPException(409, f"at most {MAX_WATCHES_PER_SUB} watches per device")
    watch = add_watch(conn, sub_id, filters, (data.get("label") or "")[:MAX_LABEL])
    return {"ok": True, "watch": {"id": watch["id"], "filters": watch["filters"],
                                  "label": watch["label"]}}


@router.post("/api/watch/unsubscribe")
async def watch_unsubscribe(request: Request):
    data = await _body(request)
    conn = _writer()
    sub_id = _require_sub(conn, _endpoint(data))
    if data.get("id"):
        cur = conn.execute("DELETE FROM watches WHERE id=? AND subscription_id=?",
                           (int(data["id"]), sub_id))
    else:
        cur = conn.execute("DELETE FROM watches WHERE subscription_id=? AND filters=?",
                           (sub_id, canonical_filters(data.get("filters") or "")))
    conn.commit()
    return {"ok": True, "deleted": cur.rowcount}


@router.post("/api/watch/rename")
async def watch_rename(request: Request):
    data = await _body(request)
    conn = _writer()
    sub_id = _require_sub(conn, _endpoint(data))
    label = (data.get("label") or "").strip()[:MAX_LABEL]
    if not label:
        raise HTTPException(400, "a watch needs a name")
    # By id from the management list, or by filters from the saved-search list,
    # which is keyed on the querystring because that is what localStorage holds.
    if data.get("id"):
        conn.execute("UPDATE watches SET label=? WHERE id=? AND subscription_id=?",
                     (label, int(data["id"]), sub_id))
    else:
        conn.execute("UPDATE watches SET label=? WHERE subscription_id=? AND filters=?",
                     (label, sub_id, canonical_filters(data.get("filters") or "")))
    conn.commit()
    return {"ok": True, "label": label}


@router.post("/api/alert/subscribe")
async def alert_subscribe(request: Request):
    """Turn on change alerts for one bookmarked tender.

    Only ever called from an explicit per-tender opt-in. The archive learns this
    single tender id and nothing else about the user's bookmark list, which
    stays in their own browser's localStorage.
    """
    data = await _body(request)
    endpoint = _endpoint(data)
    tender_id = (data.get("tender_id") or "").strip()[:100]
    if not tender_id:
        raise HTTPException(400, "tender_id is required")
    sub = data.get("subscription") or {}
    keys = sub.get("keys") or {}
    conn = _writer()
    if keys.get("p256dh") and keys.get("auth"):
        sub_id = upsert_subscription(conn, endpoint, keys["p256dh"], keys["auth"],
                                     (data.get("platform") or "")[:16] or None)
    else:
        sub_id = _require_sub(conn, endpoint)
    count = conn.execute(
        "SELECT count(*) FROM tender_alerts WHERE subscription_id=? AND active=1",
        (sub_id,)).fetchone()[0]
    if count >= MAX_ALERTS_PER_SUB:
        raise HTTPException(409, f"at most {MAX_ALERTS_PER_SUB} tender alerts per device")
    alert = add_alert(conn, sub_id, tender_id)
    if alert is None:
        raise HTTPException(404, "no such tender")
    return {"ok": True, "tender_id": tender_id}


@router.post("/api/alert/unsubscribe")
async def alert_unsubscribe(request: Request):
    data = await _body(request)
    conn = _writer()
    sub_id = _require_sub(conn, _endpoint(data))
    cur = conn.execute("DELETE FROM tender_alerts WHERE subscription_id=? AND tender_id=?",
                       (sub_id, (data.get("tender_id") or "").strip()))
    conn.commit()
    return {"ok": True, "deleted": cur.rowcount}


@router.post("/api/push/forget")
async def push_forget(request: Request):
    """Delete the subscription and every watch and alert attached to it."""
    data = await _body(request)
    conn = _writer()
    return forget(conn, _endpoint(data))


@router.post("/api/push/test")
async def push_test(request: Request):
    """Send one real notification, so a subscriber can prove delivery works.

    The round trip a browser cannot fake: this leaves the server, goes out to
    the push service, and comes back down to the device.
    """
    if _vapid is None:
        raise HTTPException(503, "push is not configured on this mirror")
    data = await _body(request)
    endpoint = _endpoint(data)
    conn = _writer()
    row = conn.execute("SELECT * FROM push_subscriptions WHERE endpoint=?",
                       (endpoint,)).fetchone()
    if row is None:
        raise HTTPException(404, "unknown subscription")
    result = push_mod.send(
        _vapid, {"endpoint": row["endpoint"],
                 "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}},
        {"kind": "test", "title": "Notifications are working",
         "body": "You will get one of these when a watched search or a "
                 "bookmarked tender changes.", "url": "/bookmarks", "tag": "test"})
    if result.action == "delete":
        conn.execute("DELETE FROM push_subscriptions WHERE id=?", (row["id"],))
        conn.commit()
        raise HTTPException(410, "this subscription is no longer valid")
    if not result.ok:
        log.info("test push to subscription %s (%s) failed: %s",
                 row["id"], push_mod.provider(row["endpoint"]), result.detail)
        raise HTTPException(502, f"push service refused it ({result.detail})")
    return {"ok": True}


@router.post("/api/watch/preview")
async def watch_preview(request: Request):
    """What a filter set would be called and stored as, without saving anything.

    The canonical form is what lets the browse button light up for a search the
    user saved by a different route — a department chip and the Advanced panel
    produce different querystrings for the same search, and only the server
    knows they reduce to one watch.
    """
    data = await _body(request)
    canon = canonical_filters((data.get("filters") or "")[:MAX_FILTERS])
    return {"label": describe(canon), "filters": canon}
