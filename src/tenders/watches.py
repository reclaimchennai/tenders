"""Saved searches, bookmark alerts, and the job that decides what is news.

Two subscription types share one pipeline:

* a **watch** is a saved /browse querystring — "tell me when a *new tender*
  matches this" — and is answered by re-running the site's own search layer;
* a **tender alert** is one bookmarked tender — "tell me when *this* changes" —
  and is answered by comparing the tender against the state it was in when the
  user asked.

Both feed the same push transport, and both are capped, but they are presented
differently on purpose. A watch sends **one notification per new tender**, each
with its own tag, its own ``/tender/<id>`` and its own summary, because they are
separate things to read, dismiss and open — a shared tag is what made five
matches arrive as one line that replaced itself. A bookmark digest stays one
notification for all of a person's changed bookmarks, because those are updates
to rows they already have, and the useful next step is the list, not each row.

Presentation is capped independently of detection: past ``max_tender_pushes``
per watch the remainder collapses into a single "…and N more" pointing at the
search, and past ``max_pushes_per_subscription`` a device simply gets nothing
further this pass. Neither cap decides *whether* a tender is news — that is the
four layers below, and they are the only thing standing between this feature and
a 20,000-row backfill.


WHAT "NEW" AND "CHANGED" HAVE TO MEAN
-------------------------------------
This archive is not a live feed of the portal; it is a mirror that is still
recovering the portal's history. At the time of writing two backfills are
inserting *tens of thousands* of 2020-2026 tenders a day, and the award sweep is
attaching contract awards dated 2025 to tenders it has never notified anybody
about. Any rule of the form "a row appeared" or "a column stopped being NULL"
would, the first time a batch landed, send thousands of notifications about
decade-old procurement. That is not a tuning problem — one such night would end
the feature's credibility permanently. So both detectors are keyed on *the
portal's own dates and the portal's own transitions*, never on ours:

**New tender** (watches). A tender is announced only if all of these hold:

1. ``published_date`` is present. The portal stated when it published this. It
   is absent on 89% of the archive and on essentially everything the status-
   listing backfills insert, so this one condition alone excludes the flood.
2. ``published_date >= watches.cutoff_at`` — it was published after the watch
   was created. A watch never speaks about the past, however long afterwards we
   discover it.
3. ``published_date`` is within ``fresh_days`` of now. An absolute floor that
   binds even a watch created a year ago, so a row that *acquires* an old
   publication date during a later detail pass still cannot fire.
4. The pair (watch, tender) is not already in ``watch_matches``. Exactly once.
5. ``status == 'detailed'`` — we have actually read the tender's detail page
   and recorded whatever documents the portal shows for it. ``published_date``
   is known from the fast "latest" listing poll, minutes after the portal
   posts it and typically hours before the slower detail+download pass
   reaches a tender with no urgent deadline; notifying on 1-4 alone sent
   people to a tender page with nothing on it yet, while the real portal
   already showed the documents. A tender held back here is not dropped, only
   deferred to whichever later pass finds it detailed.

Those are five independent layers, and a 20,000-row backfill at 3am has to
defeat all five. Layer 1 stops it outright; even a hypothetical backfill that
carried real publication dates dies on 2 and 3; even a bug in 2 and 3 leaves
layer 4 and the caps below, which turn any conceivable flood into at most five
notifications per watch per run — four tenders and a count — no more than once
an hour, and at most ``max_pushes_per_subscription`` to any one device.

**Changed tender** (bookmark alerts). A field going NULL -> value on a tender
whose detail page we had never read is this archive catching up, not news; the
same field going value -> different value is news. The distinction is made by
``tender_alerts.base_*``, a snapshot taken when the user asked, plus:

* corrigendum and cancellation changes count only if we had already read the
  tender's detail page at snapshot time (``base_status = 'detailed'``) — that is
  the only state in which "the corrigendum list grew" is a statement about the
  portal rather than about us;
* an award counts only if the portal's own ``awarded_at`` is within
  ``award_fresh_days``. Every one of the 33 awards in the archive today is dated
  2025-08 to 2026-02, so this suppresses the entire sweep while still announcing
  an award made this week;
* a re-published document counts off ``document_events.version_captured``,
  which is by construction a transition — it is written only when a file we had
  already captured came back with different bytes — and only for events dated
  after the alert was registered.

The baseline is advanced on every pass whether or not anything was sent, so
catching up is absorbed exactly once and never re-examined.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from .db import connect
from .util import now_iso
from .web.dates import closing_line
from .web.search import CRITERIA, build_match, search_tenders_advanced

log = logging.getLogger("watches")

# The portal writes its dates in India Standard Time with no offset, and
# published_date/closing_date are stored exactly as parsed. Comparing them to
# UTC would shift every judgement by 5h30m — enough to make a tender published
# at 09:00 IST look older than a watch created at 04:00 UTC the same morning.
# latest_active does the same arithmetic for the same reason.
IST = timedelta(hours=5, minutes=30)

DEFAULTS = {
    # A tender published more than a week ago is not an announcement, whatever
    # the reason we are only now looking at it.
    "fresh_days": 7,
    # An award is allowed to be older than a publication: the portal posts AOC
    # documents weeks after a tender closes, and that is genuine news. It is
    # still far short of the 2025 dates the sweep is currently backfilling.
    "award_fresh_days": 45,
    # No watch may speak more than once an hour, however much matches.
    "min_gap_minutes": 60,
    # Individual per-tender notifications one watch may raise in one pass;
    # everything past this collapses into a single "…and N more" summary.
    #
    # Four, for two reasons that happen to agree. Android's own shade groups an
    # app's notifications into a bundle at around four or five, so beyond that
    # the platform is already summarising and a fifth buzz buys nothing. And the
    # rate this has to cope with is known: the whole portal publishes on the
    # order of a few hundred tenders on a working day, which is ~25 an hour
    # across every department in Tamil Nadu, so a *filtered* watch that sees
    # five or more inside one min_gap window is not having a busy morning — it
    # is either a very wide filter or something has gone wrong upstream, and
    # both of those are better served by one line saying how many than by
    # enumerating them onto a lock screen.
    "max_tender_pushes": 4,
    # Notifications delivered to one device in one pass. Raised from 3 when the
    # unit changed: it used to count *batched* pushes, one per watch, and a
    # ceiling of three then meant three watches. One push per tender makes three
    # mean "two of your three new tenders were silently dropped". Eight is the
    # per-watch five plus room for a second watch and the bookmark-alert
    # digest. Anything past it keeps its unsent matches — they are simply not
    # written to watch_matches — and is first in line next pass.
    "max_pushes_per_subscription": 8,
    # Candidate rows examined per watch per pass. The date floor keeps the
    # window to a day or two, in which the whole portal publishes ~600 tenders,
    # so this is never reached in practice; when it is, the remainder is not
    # lost, it simply arrives on the next pass.
    "scan_limit": 1000,
    # watch_matches rows older than this are dropped. Anything that old is
    # already excluded by the freshness floor, so the row can no longer change
    # any decision.
    "prune_days": 60,
    "batch_names": 3,
}


def settings(cfg) -> dict:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg.raw.get("watch") or {}).items() if k in DEFAULTS})
    return out


def ist_now() -> datetime:
    """Portal wall-clock, naive — the frame published_date is stored in."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + IST


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

# Exactly the parameters web.app.browse accepts, split by arity. A watch that
# returned different results from the same querystring on /browse would be a
# bug, so this is a transcription of that signature and not a second opinion
# about it; test_watches asserts the two agree against a live TestClient.
_LIST_FIELDS = ("org", "category", "tender_type", "product_category",
                "form_of_contract", "payment_mode")
_SCALAR_FIELDS = ("date_from", "date_to", "captured", "tender_id", "ref_number",
                  "pincode", "value_min", "value_max")


def parse_filters(qs: str) -> dict:
    """Split a /browse querystring into search_tenders_advanced's arguments.

    Returns ``{q, filt, criteria}``. Unknown parameters — ``page``, ``scope``,
    ``partial`` and anything a future link picks up — are dropped rather than
    passed through, so a watch saved from a paginated URL still means the search
    and not page 4 of it.
    """
    pairs = parse_qsl(qs, keep_blank_values=False)
    q = ""
    filt: dict = {f: [] for f in _LIST_FIELDS}
    filt.update({f: "" for f in _SCALAR_FIELDS})
    criteria: list[str] = []
    for key, value in pairs:
        value = value.strip()
        if not value:
            continue
        if key == "q":
            q = value
        elif key in _LIST_FIELDS:
            filt[key].append(value)
        elif key in _SCALAR_FIELDS:
            filt[key] = value
        elif key == "criteria" and value in CRITERIA and value not in criteria:
            criteria.append(value)
    return {"q": q, "filt": filt, "criteria": criteria}


def canonical_filters(qs: str) -> str:
    """The querystring reduced to what actually affects the result set.

    Two people who reach the same search by different routes — one from a
    department chip, one from the Advanced panel — must end up sharing one watch
    rather than two that fire twice about the same tender, so the UNIQUE key on
    ``watches`` is over this and not over whatever the address bar happened to
    say.
    """
    parsed = parse_filters(qs)
    pairs: list[tuple[str, str]] = []
    if parsed["q"]:
        pairs.append(("q", parsed["q"]))
    for field in _LIST_FIELDS:
        pairs += [(field, v) for v in sorted(parsed["filt"][field])]
    for field in _SCALAR_FIELDS:
        if parsed["filt"][field]:
            pairs.append((field, parsed["filt"][field]))
    pairs += [("criteria", c) for c in sorted(parsed["criteria"])]
    return urlencode(pairs)


def describe(qs: str) -> str:
    """A short human label for a filter set, for the notification title."""
    parsed = parse_filters(qs)
    bits: list[str] = []
    if parsed["q"]:
        bits.append(f'“{parsed["q"]}”')
    for field in ("org", "category", "tender_type", "product_category"):
        for value in parsed["filt"][field]:
            # An organisation chain is a ||-joined path; its last segment is the
            # part a person recognises.
            bits.append(value.split("||")[-1].strip())
    if parsed["filt"]["pincode"]:
        bits.append(f'PIN {parsed["filt"]["pincode"]}')
    if parsed["filt"]["value_min"] or parsed["filt"]["value_max"]:
        bits.append("by value")
    if not bits:
        return "All new tenders"
    label = " · ".join(bits[:3])
    if len(label) > 64:
        label = label[:63] + "…"
    # Same rule as saved.js's label(): a title that names three of five filters
    # and stops describes a different search from the one that fired.
    if len(bits) > 3:
        label += f" +{len(bits) - 3} more"
    return label


# ---------------------------------------------------------------------------
# New-tender matching
# ---------------------------------------------------------------------------

def new_matches(conn, watch: dict, *, now: datetime | None = None,
                fresh_days: int = DEFAULTS["fresh_days"],
                scan_limit: int = DEFAULTS["scan_limit"]) -> list[dict]:
    """Tenders this watch should announce, newest publication first.

    The floor below is pushed into the search as ``date_from`` so the candidate
    set is a day or two of publications rather than the archive — and it is
    *intersected* with the user's own date_from, never allowed to widen it.
    """
    now = now or ist_now()
    cutoff = _parse_iso(watch["cutoff_at"]) or now
    floor = max(cutoff, now - timedelta(days=fresh_days))
    hwm = _parse_iso(watch.get("hwm_published"))
    if hwm:
        # A day of slack behind the high-water mark. Publication order and
        # discovery order differ here, so a tender published yesterday can be
        # detailed today, after today's has already been announced; the floor
        # has to look back far enough to see it, and watch_matches — not this —
        # is what stops it being announced twice.
        floor = max(floor, min(hwm - timedelta(days=1), now))

    parsed = parse_filters(watch["filters"])
    filt = dict(parsed["filt"])
    floor_day = floor.date().isoformat()
    filt["date_from"] = max(filt["date_from"], floor_day) if filt["date_from"] \
        else floor_day

    rows, _total = search_tenders_advanced(
        conn, build_match(parsed["q"]), criteria=tuple(parsed["criteria"]),
        limit=scan_limit, offset=0, **filt)

    seen = {r[0] for r in conn.execute(
        "SELECT tender_id FROM watch_matches WHERE watch_id=?", (watch["id"],))}
    fresh_floor = now - timedelta(days=fresh_days)
    out = []
    for row in rows:
        published = _parse_iso(row.get("published_date"))
        # The day-granular date_from above is a coarse pre-filter; these are the
        # actual conditions, at the precision the timestamps are stored in.
        if published is None or published < cutoff or published < fresh_floor:
            continue
        if row["tender_id"] in seen:
            continue
        # published_date is known the moment the fast "latest" listing poll
        # sees the row — minutes after the portal posts it, and long before
        # forward_capture's detail pass reaches a tender with no urgent
        # deadline. Notifying at that point sends a user to a tender page
        # with no documents on it, while the real portal already shows them.
        # status only becomes 'detailed' once we have actually read the
        # detail page and recorded whatever documents exist, so gating here
        # holds the tender back exactly until there is something to show —
        # it is not dropped, just picked up on a later pass once detailed.
        if row.get("status") != "detailed":
            continue
        out.append(row)
    out.sort(key=lambda r: (r.get("published_date") or "", r["tender_id"]),
             reverse=True)
    return out


# ---------------------------------------------------------------------------
# Per-tender change detection
# ---------------------------------------------------------------------------

def _snapshot(conn, tender_id: str) -> dict | None:
    row = conn.execute(
        "SELECT tender_id, status, corrigendum_count, corrigenda_json,"
        " cancelled_at, awarded_at, awarded_to, award_value_num, closing_date,"
        " title, short_name FROM tenders WHERE tender_id=?", (tender_id,)).fetchone()
    if row is None:
        return None
    state = dict(row)
    state["doc_versions"] = conn.execute(
        "SELECT COALESCE(sum(version_count), 0) FROM documents WHERE tender_id=?",
        (tender_id,)).fetchone()[0]
    return state


def _baseline_from(state: dict) -> dict:
    return {
        "base_status": state.get("status"),
        "base_corrigendum_count": state.get("corrigendum_count") or 0,
        "base_cancelled_at": state.get("cancelled_at"),
        "base_awarded_at": state.get("awarded_at"),
        "base_award_value": state.get("award_value_num"),
        "base_closing_date": state.get("closing_date"),
        "base_doc_versions": state.get("doc_versions") or 0,
    }


def _within(value: str | None, days: int, now: datetime) -> bool:
    """True if a portal-supplied date is recent enough to be an announcement."""
    dt = _parse_iso(value)
    if dt is None:
        # A date the portal gave us but we could not parse is not evidence of
        # recency, and guessing "recent" here is exactly how a backfill escapes.
        return False
    return dt >= now - timedelta(days=days)


def detect_changes(conn, alert: dict, *, now: datetime | None = None,
                   award_fresh_days: int = DEFAULTS["award_fresh_days"]
                   ) -> tuple[list[str], dict | None]:
    """What genuinely changed on one bookmarked tender since the baseline.

    Returns (human phrases, new baseline). An empty phrase list with a non-None
    baseline means "we learned things, none of them news" — the caller still
    stores the baseline, which is what makes catching up a one-time event.
    """
    now = now or ist_now()
    state = _snapshot(conn, alert["tender_id"])
    if state is None:
        return [], None
    events: list[str] = []
    detailed_before = alert.get("base_status") == "detailed"
    base_corr = alert.get("base_corrigendum_count")
    corr = state.get("corrigendum_count") or 0

    if detailed_before and base_corr is not None and corr > base_corr:
        kinds = _corrigendum_kinds(state.get("corrigenda_json"), base_corr)
        added = corr - base_corr
        noun = "corrigendum" if added == 1 else "corrigenda"
        events.append(f"{added} new {noun}" + (f" ({', '.join(kinds)})" if kinds else ""))

    if detailed_before and state.get("cancelled_at") and not alert.get("base_cancelled_at"):
        events.append("Cancelled by the department")

    # No detailed_before guard here, and deliberately so: awards never come from
    # the detail page (it is silent about who won), so "had we read the detail
    # page" says nothing about whether an award is new. The portal's own AOC
    # date is the only honest test, and it is the one the sweep fails.
    if state.get("awarded_at") and not alert.get("base_awarded_at"):
        if _within(state["awarded_at"], award_fresh_days, now):
            who = (state.get("awarded_to") or "").strip()
            events.append("Awarded" + (f" to {who[:60]}" if who else ""))
    elif (state.get("award_value_num") is not None
          and alert.get("base_award_value") is not None
          and state["award_value_num"] != alert["base_award_value"]):
        events.append("Award value revised")

    base_close = alert.get("base_closing_date")
    if base_close and state.get("closing_date") and state["closing_date"] != base_close:
        moved = "extended" if state["closing_date"] > base_close else "brought forward"
        events.append(f"Closing date {moved}")

    republished = conn.execute(
        "SELECT count(*) FROM document_events WHERE tender_id=? AND event='version_captured'"
        " AND at > ?", (alert["tender_id"], alert.get("baseline_at")
                        or alert["registered_at"])).fetchone()[0]
    if republished:
        noun = "document" if republished == 1 else "documents"
        events.append(f"{republished} {noun} re-published with different content")

    return events, _baseline_from(state)


def _corrigendum_kinds(corrigenda_json: str | None, skip: int) -> list[str]:
    """Types of the corrigenda beyond the first ``skip`` entries.

    The portal appends, so entries past the baseline count are the new ones. If
    it ever reorders instead, the worst case is a slightly wrong *label* on a
    notification that was correctly triggered by the count.
    """
    try:
        entries = json.loads(corrigenda_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(entries, list):
        return []
    kinds: list[str] = []
    for entry in entries[skip:]:
        if isinstance(entry, dict):
            kind = (entry.get("type") or entry.get("title") or "").strip()
            if kind and kind not in kinds:
                kinds.append(kind)
    return kinds[:3]


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def upsert_subscription(conn, endpoint: str, p256dh: str, auth: str,
                        platform: str | None = None) -> int:
    """Record (or refresh) a browser's subscription and return its id."""
    now = now_iso()
    conn.execute(
        "INSERT INTO push_subscriptions (endpoint, p256dh, auth, platform,"
        " created_at, last_seen_at) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh,"
        " auth=excluded.auth, platform=COALESCE(excluded.platform, platform),"
        " last_seen_at=excluded.last_seen_at, failures=0, retry_after=NULL",
        (endpoint, p256dh, auth, platform, now, now))
    conn.commit()
    return conn.execute("SELECT id FROM push_subscriptions WHERE endpoint=?",
                        (endpoint,)).fetchone()[0]


def subscription_id(conn, endpoint: str) -> int | None:
    row = conn.execute("SELECT id FROM push_subscriptions WHERE endpoint=?",
                       (endpoint,)).fetchone()
    return row[0] if row else None


def add_watch(conn, sub_id: int, filters: str, label: str = "") -> dict:
    """Create a watch, or return the existing one for the same filters."""
    canon = canonical_filters(filters)
    label = (label or describe(canon)).strip()[:120]
    now_utc = now_iso()
    conn.execute(
        "INSERT INTO watches (subscription_id, filters, label, created_at, cutoff_at)"
        " VALUES (?,?,?,?,?) ON CONFLICT(subscription_id, filters)"
        " DO UPDATE SET active=1, label=excluded.label",
        (sub_id, canon, label, now_utc, ist_now().replace(microsecond=0).isoformat()))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM watches WHERE subscription_id=? AND filters=?",
        (sub_id, canon)).fetchone()
    return dict(row)


def add_alert(conn, sub_id: int, tender_id: str) -> dict | None:
    """Register a change alert for one tender, baselined at its current state."""
    state = _snapshot(conn, tender_id)
    if state is None:
        return None
    base = _baseline_from(state)
    now_utc = now_iso()
    conn.execute(
        "INSERT INTO tender_alerts (subscription_id, tender_id, registered_at,"
        " baseline_at, base_status, base_corrigendum_count, base_cancelled_at,"
        " base_awarded_at, base_award_value, base_closing_date, base_doc_versions)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(subscription_id, tender_id) DO UPDATE SET active=1",
        (sub_id, tender_id, now_utc, now_utc, base["base_status"],
         base["base_corrigendum_count"], base["base_cancelled_at"],
         base["base_awarded_at"], base["base_award_value"],
         base["base_closing_date"], base["base_doc_versions"]))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM tender_alerts WHERE subscription_id=? AND tender_id=?",
        (sub_id, tender_id)).fetchone()
    return dict(row)


def forget(conn, endpoint: str) -> dict:
    """Delete a subscription and everything hanging off it.

    Real deletion, not deactivation: the cascade takes the watches, the alerts
    and the per-watch match history with it, and the endpoint — the only
    identifying thing here — is gone from the database.
    """
    row = conn.execute("SELECT id FROM push_subscriptions WHERE endpoint=?",
                       (endpoint,)).fetchone()
    if not row:
        return {"deleted": 0}
    conn.execute("DELETE FROM push_subscriptions WHERE id=?", (row[0],))
    conn.commit()
    return {"deleted": 1}


def drop_subscription(conn, sub_id: int) -> None:
    conn.execute("DELETE FROM push_subscriptions WHERE id=?", (sub_id,))
    conn.commit()


def prune(conn, *, days: int = DEFAULTS["prune_days"]) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cur = conn.execute("DELETE FROM watch_matches WHERE notified_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

def _tender_line(row: dict) -> str:
    from .shortnames import headline

    try:
        text = headline(row)
    except Exception:  # noqa: BLE001 - a label must never break a notification
        text = row.get("title") or row["tender_id"]
    text = " ".join((text or "").split())
    return text[:80] + "…" if len(text) > 80 else text


def _tender_summary(row: dict, watch: dict) -> str:
    """The two or three lines under a tender's headline in the shade.

    Whose tender, for how much, and by when — the three things that decide
    whether a notification is worth opening. The watch's own name is last
    because it answers "why am I seeing this", which is a question people only
    ask once.
    """
    bits: list[str] = []
    # Same choice describe() makes about an organisation chain: the tail is the
    # office a person recognises, the head is a directorate they may not.
    chain = (row.get("organisation_chain") or "").split("||")
    if chain and chain[-1].strip():
        bits.append(chain[-1].strip())
    value = (row.get("tender_value_raw") or "").strip()
    if value and value not in ("NA", "Nil"):
        bits.append(f"₹{value}")
    lines = [" · ".join(bits)] if bits else []
    closing = closing_line(row.get("closing_date"))
    if closing:
        lines.append(closing)
    lines.append(f"Alert: {watch['label']}")
    return "\n".join(lines)


def _watch_payloads(watch: dict, matches: list[dict],
                    max_tender_pushes: int, batch_names: int
                    ) -> list[tuple[dict, list[dict]]]:
    """One notification per tender, plus a summary for whatever is left over.

    Returns ``(payload, matches it covers)`` pairs; the caller sends them in
    order and records only what it actually delivered, so a pair cut off by the
    per-device ceiling is re-offered next pass rather than lost.

    A tender gets its own ``tag`` — Android replaces a notification that shares
    a tag with a live one, which is why five matches used to arrive as one
    line — its own ``url``, so a tap lands on the tender and not on a result
    page that has to be searched for it again, and its own ``topic``, so the
    push service collapses re-sends *about the same tender* and nothing else.
    Tender ids are URL-safe and at most 20 characters, so the topic is never
    truncated into a collision with another tender's.
    """
    out: list[tuple[dict, list[dict]]] = []
    for match in matches[:max_tender_pushes]:
        tid = match["tender_id"]
        out.append(({"kind": "watch", "title": _tender_line(match)[:120],
                     "body": _tender_summary(match, watch),
                     "url": f"/tender/{tid}", "tag": f"tender-{tid}",
                     "topic": f"t{tid}", "count": 1}, [match]))
    rest = matches[max_tender_pushes:]
    if rest:
        # The remainder is a count and a link, never a list of names: it exists
        # because enumerating them was already judged too much.
        body = "\n".join(_tender_line(m) for m in rest[:batch_names])
        if len(rest) > batch_names:
            body += f"\n…and {len(rest) - batch_names} more"
        out.append(({"kind": "watch",
                     "title": f"{len(rest)} more new tenders: {watch['label']}"[:120],
                     "body": body, "url": "/browse?" + watch["filters"],
                     "tag": f"watch-{watch['id']}", "count": len(rest)}, rest))
    return out


def _alert_payload(changes: list[tuple[dict, list[str]]], batch_names: int) -> dict:
    """One push for everything that changed on this subscriber's bookmarks."""
    if len(changes) == 1:
        alert, events = changes[0]
        return {"kind": "alert",
                "title": f"Tender updated: {_tender_line(alert['_state'])}"[:120],
                "body": "; ".join(events),
                "url": f"/tender/{alert['tender_id']}",
                "tag": f"alert-{alert['tender_id']}", "count": 1}
    lines = [f"{_tender_line(a['_state'])}: {'; '.join(e)}"
             for a, e in changes[:batch_names]]
    if len(changes) > batch_names:
        lines.append(f"…and {len(changes) - batch_names} more")
    return {"kind": "alert",
            "title": f"{len(changes)} bookmarked tenders changed",
            "body": "\n".join(lines), "url": "/bookmarks",
            "tag": "alerts", "count": len(changes)}


def run_watches(db_path: Path | None = None, *, cfg=None, dry_run: bool = False,
                now: datetime | None = None) -> dict:
    """One pass: match every watch, diff every alert, send batched pushes.

    Pure local work apart from the outbound pushes — it never touches the
    portal, so it costs the scraper's politeness budget nothing. It is also why
    this runs on its own timer rather than inside a capture cycle: a push
    service that hangs must delay nobody's document capture.
    """
    from .config import load_config
    from . import push as push_mod

    cfg = cfg or load_config()
    db_path = db_path or cfg.db_path
    opts = settings(cfg)
    now = now or ist_now()
    now_utc = datetime.now(timezone.utc)
    vapid = None if dry_run else push_mod.load_vapid(cfg)
    if vapid is None and not dry_run:
        log.warning("no VAPID key configured; nothing can be delivered")
        return {"skipped": "no vapid key"}

    conn = connect(db_path)
    stats = {"subscriptions": 0, "watches": 0, "alerts": 0, "matches": 0,
             "changes": 0, "pushes": 0, "failed": 0, "dropped": 0, "deferred": 0}
    try:
        subs = [dict(r) for r in conn.execute(
            "SELECT * FROM push_subscriptions"
            " WHERE retry_after IS NULL OR retry_after <= ?",
            (now_utc.isoformat(),))]
        stats["subscriptions"] = len(subs)
        for sub in subs:
            # Two queues, drained alerts-first. A bookmarked tender being
            # cancelled or awarded is news about something the reader went out
            # of their way to follow; a new search match is news about a filter.
            # When the per-device ceiling bites, the filter is what should lose.
            watch_queue: list[tuple[dict, dict]] = []   # (payload, commit-info)
            alert_queue: list[tuple[dict, dict]] = []

            for row in conn.execute(
                    "SELECT * FROM watches WHERE subscription_id=? AND active=1",
                    (sub["id"],)):
                watch = dict(row)
                stats["watches"] += 1
                last = _parse_iso(watch.get("last_notified_at"))
                if last and last > now_utc.replace(tzinfo=None) - timedelta(
                        minutes=opts["min_gap_minutes"]):
                    continue
                matches = new_matches(conn, watch, now=now,
                                      fresh_days=opts["fresh_days"],
                                      scan_limit=opts["scan_limit"])
                if not matches:
                    conn.execute("UPDATE watches SET last_checked_at=? WHERE id=?",
                                 (now_utc.isoformat(), watch["id"]))
                    continue
                stats["matches"] += len(matches)
                pairs = _watch_payloads(watch, matches,
                                        int(opts["max_tender_pushes"]),
                                        opts["batch_names"])
                watch_queue += [(payload, {"watch": watch, "matches": covered})
                                for payload, covered in pairs]
                # The high-water mark may only pass a tender we have actually
                # spoken about, so it moves once, on the last notification of
                # this watch's set, and to the newest date in the whole set. If
                # the device's ceiling cuts the set short the mark stays put and
                # the floor keeps looking far enough back to find the remainder
                # next pass — otherwise the oldest matches would drop below a
                # floor raised on their behalf and never be seen again.
                watch_queue[-1][1]["hwm_matches"] = matches

            changed: list[tuple[dict, list[str]]] = []
            for row in conn.execute(
                    "SELECT * FROM tender_alerts WHERE subscription_id=? AND active=1",
                    (sub["id"],)):
                alert = dict(row)
                stats["alerts"] += 1
                events, baseline = detect_changes(
                    conn, alert, now=now, award_fresh_days=opts["award_fresh_days"])
                if baseline is None:
                    continue
                # Written whether or not anything is announced: an unannounced
                # difference is this archive catching up, and it must be absorbed
                # so the next pass does not re-examine it.
                conn.execute(
                    "UPDATE tender_alerts SET baseline_at=?, base_status=?,"
                    " base_corrigendum_count=?, base_cancelled_at=?, base_awarded_at=?,"
                    " base_award_value=?, base_closing_date=?, base_doc_versions=?"
                    " WHERE id=?",
                    (now_utc.isoformat(), baseline["base_status"],
                     baseline["base_corrigendum_count"], baseline["base_cancelled_at"],
                     baseline["base_awarded_at"], baseline["base_award_value"],
                     baseline["base_closing_date"], baseline["base_doc_versions"],
                     alert["id"]))
                if events:
                    alert["_state"] = _snapshot(conn, alert["tender_id"]) or alert
                    changed.append((alert, events))
            if changed:
                stats["changes"] += len(changed)
                alert_queue.append((_alert_payload(changed, opts["batch_names"]),
                                    {"alerts": [a for a, _ in changed]}))
            conn.commit()

            queue = alert_queue + watch_queue
            cap = int(opts["max_pushes_per_subscription"])
            stats["deferred"] += max(0, len(queue) - cap)
            for payload, info in queue[:cap]:
                if dry_run:
                    stats["pushes"] += 1
                    _commit_sent(conn, info, now_utc)
                    continue
                result = push_mod.send(
                    vapid,
                    {"endpoint": sub["endpoint"],
                     "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                    payload,
                    topic=payload.get("topic")
                    or payload["tag"].replace("-", "")[:32])
                _apply_result(conn, sub, result, now_utc, stats)
                if result.ok:
                    stats["pushes"] += 1
                    _commit_sent(conn, info, now_utc)
                if result.action in ("delete", "backoff"):
                    break
            conn.commit()

        stats["pruned"] = prune(conn, days=opts["prune_days"])
    finally:
        conn.close()
    return stats


def _commit_sent(conn, info: dict, now_utc: datetime) -> None:
    """Record what a delivered push covered, so it is never repeated."""
    stamp = now_utc.isoformat()
    watch = info.get("watch")
    if watch:
        matches = info["matches"]
        conn.executemany(
            "INSERT OR IGNORE INTO watch_matches (watch_id, tender_id, notified_at)"
            " VALUES (?,?,?)",
            [(watch["id"], m["tender_id"], stamp) for m in matches])
        # Clamped to now: a handful of tenders carry a publication date weeks in
        # the future (portal data entry), and letting one of those become the
        # high-water mark would blind the watch until that date arrived.
        published = [m.get("published_date") for m in info.get("hwm_matches", ())
                     if m.get("published_date")]
        hwm = min(max(published), ist_now().isoformat()) if published else None
        conn.execute(
            "UPDATE watches SET last_notified_at=?, last_checked_at=?,"
            " notified_count=notified_count+?, hwm_published=MAX(COALESCE(hwm_published,''), ?)"
            " WHERE id=?",
            (stamp, stamp, len(matches), hwm or "", watch["id"]))
    for alert in info.get("alerts", []):
        conn.execute(
            "UPDATE tender_alerts SET last_notified_at=?,"
            " notified_count=notified_count+1 WHERE id=?", (stamp, alert["id"]))


def _apply_result(conn, sub: dict, result, now_utc: datetime, stats: dict) -> None:
    from . import push as push_mod

    if result.ok:
        conn.execute(
            "UPDATE push_subscriptions SET failures=0, retry_after=NULL, last_push_at=?"
            " WHERE id=?", (now_utc.isoformat(), sub["id"]))
        return
    host = push_mod.provider(sub["endpoint"])
    if result.action == "delete":
        drop_subscription(conn, sub["id"])
        stats["dropped"] += 1
        log.info("subscription %s (%s) is gone (%s); deleted with its watches",
                 sub["id"], host, result.status)
        return
    if result.action == "backoff":
        until = (now_utc + timedelta(seconds=result.retry_after_s)).isoformat()
        conn.execute("UPDATE push_subscriptions SET retry_after=? WHERE id=?",
                     (until, sub["id"]))
        log.info("subscription %s (%s) rate limited; backing off %ss",
                 sub["id"], host, result.retry_after_s)
        return
    stats["failed"] += 1
    failures = (sub.get("failures") or 0) + 1
    conn.execute("UPDATE push_subscriptions SET failures=? WHERE id=?",
                 (failures, sub["id"]))
    if failures >= push_mod.MAX_FAILURES:
        drop_subscription(conn, sub["id"])
        stats["dropped"] += 1
        log.info("subscription %s (%s) failed %d times running; deleted",
                 sub["id"], host, failures)
    else:
        log.debug("subscription %s (%s) push failed: %s", sub["id"], host, result.detail)
