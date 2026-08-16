"""Newest-published-first polling of "Latest Active Tenders" — the short-window
detector.

The archive's flagship corruption signal is the *short bidding window*: a tender
published and closed within hours, leaving no time for a genuine competitor to
respond. Discovery via the organisation tree cannot see those tenders at all.
The tree lists only currently-open tenders and a full pass costs a few hundred
requests followed by hours of detail/document work, so the effective revisit
period is measured in hours — a tender that opens at 17:00 and closes at 17:23
is born and dies inside one pass. Every such tender is invisible, and they are
exactly the ones worth catching.

``FrontEndLatestActiveTenders`` fixes that. It is captcha-gated, but a blank
search sorted by "Published Date" returns every active tender newest-first, ten
per page, and — unlike the organisation tree — each row already carries the
e-Published, closing and opening timestamps plus the tender value. So a single
captcha solve plus one page read is enough to learn about everything published
since the previous poll, dates included. Two requests, once every few minutes,
against a tree walk of several hundred.

Three properties make this work:

* **Depth is adaptive.** Page 1 covers the ten newest tenders; at the observed
  peak publication rate (~40/hour) that is fifteen minutes of history, but a
  burst can outrun it. So the walk keeps paging while a page still contains
  something unknown and stops on the first page that is entirely known —
  self-tuning between one page when quiet and ``max_pages`` in a burst.
* **Rows carry a reusable permalink.** The row's session-bound ``$DirectLink``
  differs from the stable one only in its ``page=`` parameter; rewriting it to
  ``FrontEndViewTender`` and dropping ``session=T`` yields exactly the permalink
  form the organisation-tree walker stores, verified against a live fetch in a
  fresh session. Discovery here therefore needs no follow-up request to become
  addressable later.
* **Urgent tenders jump the queue.** A tender whose documents will be deleted
  within the hour cannot wait for the next forward pass, so the poll captures
  detail + documents inline, soonest-closing first, bounded per poll.

Politeness and runaway safety: everything goes through the shared ``HttpClient``
(so the configured min-interval applies), and because ``max_requests_per_run``
is 0 (unlimited) in this deployment the poller carries its own budget — a
per-poll page cap, a per-poll capture cap, and a rolling per-hour request
ceiling in ``LatestWatch``.
"""

from __future__ import annotations

import html as htmllib
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from .captcha import save_verified_label, solve_image
from .config import load_config
from .db import connect, init_db
from .doc_lifecycle import next_attempt_after
from .http_client import HttpClient, RequestCapExceeded
from .jsf import extract_form
from .parse_listing import parse_result_rows, sp_token
from .redflags import check_and_flag
from .util import now_iso, parse_date, parse_money

log = logging.getLogger("latest")

_TENDER_ID = re.compile(r"\d{4}_[A-Za-z]+_\d+_\d+")
_LOAD_NEXT = re.compile(r'id="loadNext"[^>]*href="([^"]+)"')
# Tapestry's rewind-failure page, and the one line on it that says what it
# wanted. Distinguishing it from a wrong captcha matters: they look identical to
# a "did any tender ids come back?" test, but one is worth retrying and the
# other is a code bug that will fail identically forever.
_STALE_LINK = re.compile(r"<title>\s*Stale Link\s*</title>", re.I)
_REWIND_DETAIL = re.compile(r'id="Insert"[^>]*>(.*?)</span>', re.S)

# The two "latest …" surfaces are the same page furniture with a different last
# column, so one parser and one search helper serve both.
LATEST_TENDERS = {"page": "FrontEndLatestActiveTenders",
                  "form": "LatestActiveTenders"}
LATEST_CORRIGENDA = {"page": "FrontEndLatestActiveCorrigendums",
                     "form": "LatestActiveCorrigendums"}

# Value of the "Select Sorting Option" radio for Published Date. Nothing is
# checked by default and jsf.extract_form takes the last option (Tender ID), so
# this must always be set explicitly — sorting by tender id would defeat the
# entire point of the poll.
_SORT_PUBLISHED = "0"

IST = timedelta(hours=5, minutes=30)

# Defaults for the [latest] config block.
_DEFAULTS = {
    "enabled": True,
    "poll_interval_s": 300,
    "max_pages": 8,
    "captcha_attempts": 6,
    "max_requests_per_hour": 180,
    "max_backoff_s": 3600,
    "urgent_window_hours": 24.0,
    "urgent_remaining_hours": 6.0,
    "max_urgent_captures": 8,
    "late_link_window_hours": 3.0,
    "recapture_min_s": 600,
    "corrigendum_interval_s": 21600,
    "corrigendum_max_pages": 0,
    "corrigendum_max_captures": 25,
}


def settings(cfg) -> dict:
    out = dict(_DEFAULTS)
    out.update(cfg.raw.get("latest", {}))
    return out


def _ist_now() -> datetime:
    """Portal timestamps are naive IST wall-clock; comparisons need the same."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + IST


def _hours_between(a: str | None, b: str | None) -> float | None:
    # TypeError as well as ValueError: CSV-imported rows can carry an offset
    # where scraped ones are naive, and mixing the two subtracts to an error.
    if not a or not b:
        return None
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def parse_latest_rows(html_text: str, host: str) -> list[dict]:
    """Return the result rows of either "latest …" listing.

    Same table shape as an organisation drill-down, so the row parser is shared
    (see parse_listing.parse_result_rows).
    """
    soup = BeautifulSoup(html_text, "lxml")
    table = soup.find("table", id="table")
    return parse_result_rows(table, host) if table is not None else []


def _search_form(client: HttpClient, cfg, surface: dict) -> dict | None:
    """Render the search page with any previous results discarded, and parse it.

    Entering through the page's own "Clear" link rather than through
    ``service=page`` is what makes this surface pollable at all. Tapestry keeps
    the result table in the page's *session* state, so once a search has
    succeeded every later render of the page — ``service=page`` included —
    comes back with the rows still in it, and its ``formids`` grows from the
    bare form's 10 component ids to 25 (``iterRows_0``, ``If_23``…``If_49``).
    Echoing that longer list back makes the server's form rewind expect
    components our POST has no way to supply, and it answers "Stale Link"
    *before it ever looks at the captcha*.

    That is why exactly the first poll of a session used to work and every
    later one failed on all six attempts however good the answer was — a
    failure that reads as a captcha problem in the log and is not one. Clearing
    costs nothing: it replaces the GET we were doing anyway.
    """
    resp = client.get(cfg.host + f"/nicgep/app?component=clear"
                                 f"&page={surface['page']}&service=direct&session=T")
    return extract_form(resp.text, surface["form"])


def _search(client: HttpClient, cfg, surface: dict, *, sort: str | None = None,
            attempts: int = 4, progress=None) -> str | None:
    """Solve the captcha and POST a blank search; return the first result page.

    A rejected captcha re-renders the bare search form, which carries no result
    table, so the presence of tender-id rows is the success test — the page
    embeds an "Invalid Captcha!" string in a JS validation template whatever the
    outcome, as on the other captcha-gated surfaces.
    """
    _p = progress or (lambda m: log.debug(m))
    for i in range(1, attempts + 1):
        form = _search_form(client, cfg, surface)
        if not form or not form.get("captcha_src"):
            _p(f"  {surface['page']}: no captcha form (attempt {i})")
            continue
        # tracked=False: a poll every five minutes with six attempts is the
        # loudest captcha consumer in the process, so if this surface ever
        # breaks again it must not be able to convince the solver that the
        # *model* is broken and demote the document downloads with it.
        # Confirming below still clears the breaker — trust is one-way here.
        solution = solve_image(form["captcha_src"], tracked=False)
        if not solution:
            continue
        fields = dict(form["fields"])
        fields.update({"TenderId": "", "TenderTitle": "", "captchaText": solution,
                       "Submit": "Search", "submitmode": "S", "submitname": "Submit"})
        if sort is not None:
            fields["size"] = sort
        page = client.post(cfg.host + form["action"], data=fields)
        if _TENDER_ID.search(page.text):
            # A result table means the portal accepted the solve, so this is a
            # server-checked training label — free, and the only kind the
            # trainer can validate on.
            save_verified_label(form["captcha_src"], solution, cfg)
            return page.text
        if _STALE_LINK.search(page.text):
            detail = _REWIND_DETAIL.search(page.text)
            log.warning("%s: form rejected as a stale link, not a bad captcha (%s)",
                        surface["page"],
                        detail.group(1).strip() if detail else "no detail given")
            _p(f"  {surface['page']}: stale-link rejection (attempt {i})")
            continue
        _p(f"  {surface['page']}: captcha rejected (attempt {i})")
    _p(f"  {surface['page']}: search failed after {attempts} attempts")
    return None


def record_row(conn, row: dict) -> tuple[str, dict]:
    """Upsert one listing row; return (outcome, stored row).

    ``outcome`` is "new", "enriched" (the tender was known but undateable until
    now) or "known" — the paging walk uses it to decide whether the page taught
    it anything.

    Dates are the reason this surface exists, so they are written even for
    tenders another surface already discovered. ``published_date`` is fixed for
    the life of a tender and is only ever filled in, but closing/opening dates
    move when a corrigendum extends a deadline, so a live listing value wins
    over a stored one. Everything else defers to whatever is already held, since
    the detail page is the richer source.
    """
    ts = now_iso()
    pub = parse_date(row["published_raw"])
    close = parse_date(row["closing_raw"])
    opening = parse_date(row["opening_raw"])
    prior = conn.execute(
        "SELECT published_date FROM tenders WHERE tender_id = ?",
        (row["tender_id"],)).fetchone()
    if prior is None:
        outcome = "new"
    elif prior["published_date"] is None and pub is not None:
        outcome = "enriched"
    else:
        outcome = "known"
    # Every fresh row is due for its first capture attempt in a minute — see
    # doc_lifecycle.RETRY_SCHEDULE_S, the same progressive schedule
    # capture_retry.py advances this on for every retry after. On conflict,
    # COALESCE only fills this in when it is NULL: a tender already mid-schedule
    # keeps its own next attempt time, and one already completed (cleared back
    # to NULL by capture_retry once detailed with nothing outstanding) is left
    # alone rather than being restarted for no reason.
    next_cap = next_attempt_after(1, now=datetime.fromisoformat(ts))
    conn.execute(
        """
        INSERT INTO tenders (tender_id, title, reference_number, organisation_chain,
            published_date, published_date_raw, closing_date, closing_date_raw,
            opening_date, opening_date_raw, tender_value_raw, tender_value_num,
            detail_url, sp_token, source, status, next_capture_at,
            first_seen_at, last_updated_at)
        VALUES (:tid, :title, :ref, :org, :pub, :pub_raw, :close, :close_raw,
                :open, :open_raw, :val_raw, :val_num, :url, :sp,
                'scraped', 'discovered', :next_cap, :ts, :ts)
        ON CONFLICT(tender_id) DO UPDATE SET
            title = COALESCE(tenders.title, excluded.title),
            reference_number = COALESCE(tenders.reference_number, excluded.reference_number),
            organisation_chain = COALESCE(tenders.organisation_chain, excluded.organisation_chain),
            published_date = COALESCE(tenders.published_date, excluded.published_date),
            published_date_raw = COALESCE(tenders.published_date_raw, excluded.published_date_raw),
            closing_date = COALESCE(excluded.closing_date, tenders.closing_date),
            closing_date_raw = COALESCE(excluded.closing_date_raw, tenders.closing_date_raw),
            opening_date = COALESCE(excluded.opening_date, tenders.opening_date),
            opening_date_raw = COALESCE(excluded.opening_date_raw, tenders.opening_date_raw),
            tender_value_raw = COALESCE(tenders.tender_value_raw, excluded.tender_value_raw),
            tender_value_num = COALESCE(tenders.tender_value_num, excluded.tender_value_num),
            detail_url = COALESCE(tenders.detail_url, excluded.detail_url),
            sp_token = COALESCE(tenders.sp_token, excluded.sp_token),
            next_capture_at = COALESCE(tenders.next_capture_at, excluded.next_capture_at)
        """,
        {"tid": row["tender_id"], "title": row["title"], "ref": row["reference_number"],
         "org": row["organisation_chain"], "pub": pub, "pub_raw": row["published_raw"],
         "close": close, "close_raw": row["closing_raw"],
         "open": opening, "open_raw": row["opening_raw"],
         "val_raw": row["value_raw"], "val_num": parse_money(row["value_raw"]),
         "url": row["detail_url"], "sp": sp_token(row["detail_url"]),
         "next_cap": next_cap, "ts": ts},
    )
    stored = conn.execute(
        "SELECT published_date, closing_date, status, last_updated_at, detail_url"
        " FROM tenders WHERE tender_id = ?", (row["tender_id"],)).fetchone()
    check_and_flag(conn, row["tender_id"], stored["published_date"],
                   stored["closing_date"])
    return outcome, dict(stored)


def _is_urgent(stored: dict, opts: dict) -> float | None:
    """Hours until close if this tender must be captured now, else None.

    Two disjoint reasons to jump the queue: the bidding window is short enough
    to be a red flag in its own right (the tender we are hunting), or the
    deadline is simply near — either way the portal deletes the documents at
    close and the next forward pass is hours away.
    """
    closing = stored.get("closing_date")
    if not closing:
        return None
    try:
        remaining = (datetime.fromisoformat(closing) - _ist_now()).total_seconds() / 3600
    except ValueError:
        return None
    if remaining <= 0:
        return None
    window = _hours_between(stored.get("published_date"), closing)
    if (window is not None and window < float(opts["urgent_window_hours"])) \
            or remaining < float(opts["urgent_remaining_hours"]):
        return remaining
    return None


def _needs_capture(conn, tender_id: str, stored: dict, opts: dict) -> bool:
    """Whether an urgent tender is worth (re-)fetching right now.

    Never detailed is the obvious case. The subtle one is a tender detailed
    minutes after publication, before the portal had attached its files: the
    detail page showed the filenames as plain text, so pipeline recorded them
    'lost', and on a short-window tender the normal retry backoff will not come
    round again before the deadline. Re-probing is therefore allowed while the
    tender is young enough for links still to be appearing, spaced by
    ``recapture_min_s`` so a genuinely file-less tender costs a bounded handful
    of requests rather than one per poll for its whole life.
    """
    if stored.get("status") != "detailed":
        return True
    outstanding = conn.execute(
        "SELECT 1 FROM documents WHERE tender_id = ? AND status IN"
        " ('pending','failed','lost') LIMIT 1", (tender_id,)).fetchone()
    if not outstanding:
        return False
    age = _hours_between(stored.get("published_date"), _ist_now().isoformat())
    if age is not None and age > float(opts["late_link_window_hours"]):
        return False
    since = _hours_between(stored.get("last_updated_at"), now_iso())
    return since is None or since * 3600 >= float(opts["recapture_min_s"])


def poll_latest(conn, client: HttpClient, cfg, *, max_pages: int | None = None,
                capture: bool = True, progress=None) -> dict:
    """One newest-first sweep of the active listing, plus urgent capture."""
    _p = progress or (lambda m: log.info(m))
    opts = settings(cfg)
    max_pages = opts["max_pages"] if max_pages is None else max_pages
    start_requests = client.request_count

    html_text = _search(client, cfg, LATEST_TENDERS, sort=_SORT_PUBLISHED,
                        attempts=int(opts["captcha_attempts"]), progress=_p)
    if html_text is None:
        return {"ok": False, "rows": 0, "new": 0, "pages": 0,
                "requests": client.request_count - start_requests}

    rows_seen = new_total = enriched_total = pages = 0
    urgent: list[tuple[float, str, dict]] = []
    while True:
        rows = parse_latest_rows(html_text, cfg.host)
        pages += 1
        rows_seen += len(rows)
        learned = 0
        for row in rows:
            outcome, stored = record_row(conn, row)
            new_total += outcome == "new"
            enriched_total += outcome == "enriched"
            learned += outcome != "known"
            hrs = _is_urgent(stored, opts)
            if hrs is not None and stored.get("detail_url") \
                    and _needs_capture(conn, row["tender_id"], stored, opts):
                urgent.append((hrs, row["tender_id"], stored))
        conn.commit()
        # Stop on the first page that taught us nothing. By the newest-first
        # ordering, anything published since the last poll is above such a page.
        # "Taught us nothing" includes filling in a date, so the walk also
        # chases down tenders the organisation tree discovered without one —
        # which collapses back to a single page once that backlog is gone.
        if not learned or not rows:
            break
        if max_pages and pages >= max_pages:
            _p(f"  latest: page cap ({max_pages}) reached with new rows still coming")
            break
        nxt = _LOAD_NEXT.search(html_text)
        if not nxt:
            break
        resp = client.get(cfg.host + htmllib.unescape(nxt.group(1)))
        if resp.status_code != 200:
            break
        html_text = resp.text

    summary = {"ok": True, "rows": rows_seen, "new": new_total, "pages": pages,
               "enriched": enriched_total, "urgent": len(urgent),
               "captured": 0, "docs_captured": 0}
    if capture and urgent:
        summary.update(_capture_urgent(conn, client, cfg, urgent, opts, _p))
    summary["requests"] = client.request_count - start_requests
    if new_total or enriched_total or urgent:
        _p(f"  latest: {rows_seen} rows / {pages} pages, {new_total} new, "
           f"{enriched_total} dated, {len(urgent)} urgent, "
           f"{summary['docs_captured']} docs")
    return summary


def _capture_urgent(conn, client: HttpClient, cfg, urgent: list, opts: dict,
                    _p) -> dict:
    """Detail + document capture for closing-soonest tenders, bounded per poll.

    Ordered by time left rather than by discovery order: whichever tender the
    portal is about to strip first is the one we cannot come back for.
    """
    from .pipeline import fetch_and_store_detail

    urgent.sort(key=lambda u: u[0])
    limit = int(opts["max_urgent_captures"])
    captured = docs = 0
    for hrs, tender_id, stored in urgent[:limit] if limit else urgent:
        _p(f"  latest: URGENT {tender_id} closes in {hrs:.1f}h — capturing now")
        try:
            res = fetch_and_store_detail(conn, client, cfg, tender_id,
                                         stored["detail_url"], download=True)
        except RequestCapExceeded:
            _p("  latest: request cap hit during urgent capture")
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("urgent capture failed for %s: %s", tender_id, exc)
            continue
        captured += 1 if res.get("ok") else 0
        docs += (res.get("downloaded") or {}).get("captured", 0)
    if limit and len(urgent) > limit:
        # The rest are not dropped: they now carry a closing_date, and the
        # forward pass orders its queue by exactly that.
        _p(f"  latest: {len(urgent) - limit} more urgent tenders left to the forward pass")
    return {"captured": captured, "docs_captured": docs}


def poll_corrigendums(conn, client: HttpClient, cfg, *, max_pages: int | None = None,
                      capture: bool = True, progress=None) -> dict:
    """Sweep "Latest Active Corrigendums" and refresh the tenders it names.

    A corrigendum is the portal announcing a mid-tender amendment — a changed
    deadline, a replaced document — which is precisely the kind of modification
    the archive exists to witness. Unlike the tender listing this one cannot be
    polled shallowly: its rows are ordered by the *tender's* publication date,
    not the corrigendum's, so a fresh amendment to an older tender appears deep
    in the list and page 1 says nothing about it. The whole list is small
    (a few hundred rows) and changes slowly, so the sweep is full but
    infrequent, and the work it triggers is a detail re-fetch only for tenders
    we have no corrigendum on record for.
    """
    _p = progress or (lambda m: log.info(m))
    opts = settings(cfg)
    max_pages = opts["corrigendum_max_pages"] if max_pages is None else max_pages
    start_requests = client.request_count

    html_text = _search(client, cfg, LATEST_CORRIGENDA,
                        attempts=int(opts["captcha_attempts"]), progress=_p)
    if html_text is None:
        return {"ok": False, "rows": 0, "new": 0, "pages": 0,
                "requests": client.request_count - start_requests}

    rows_seen = new_total = pages = 0
    # (priority, tender_id, stored) — never-detailed tenders go first.
    stale: list[tuple[int, str, dict]] = []
    while True:
        rows = parse_latest_rows(html_text, cfg.host)
        pages += 1
        rows_seen += len(rows)
        for row in rows:
            outcome, stored = record_row(conn, row)
            new_total += outcome == "new"
            # The listing names the tender but not the amendment, so "do we
            # already hold a corrigendum for this tender" is the only available
            # dedupe. Once the re-fetch stores corrigenda_json the tender stops
            # matching and the sweep leaves it alone. The 24h floor is for the
            # case where it never stops matching — a listed amendment that the
            # detail page does not show would otherwise be re-fetched on every
            # sweep forever.
            if not stored.get("detail_url"):
                continue
            known = conn.execute(
                "SELECT corrigendum_count FROM tenders WHERE tender_id = ?",
                (row["tender_id"],)).fetchone()
            undetailed = stored.get("status") != "detailed"
            age = _hours_between(stored.get("last_updated_at"), now_iso())
            if undetailed or ((not known or not known["corrigendum_count"])
                              and (age is None or age >= 24)):
                stale.append((0 if undetailed else 1, row["tender_id"], stored))
        conn.commit()
        if max_pages and pages >= max_pages:
            break
        nxt = _LOAD_NEXT.search(html_text)
        if not nxt:
            break
        resp = client.get(cfg.host + htmllib.unescape(nxt.group(1)))
        if resp.status_code != 200:
            break
        html_text = resp.text

    summary = {"ok": True, "rows": rows_seen, "new": new_total, "pages": pages,
               "amended": len(stale), "refreshed": 0, "docs_captured": 0}
    if capture and stale:
        from .pipeline import fetch_and_store_detail

        limit = int(opts["corrigendum_max_captures"])
        stale.sort(key=lambda s: s[0])
        for _, tender_id, stored in (stale[:limit] if limit else stale):
            try:
                res = fetch_and_store_detail(conn, client, cfg, tender_id,
                                             stored["detail_url"], download=True)
            except RequestCapExceeded:
                _p("  corrigendum: request cap hit")
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("corrigendum refresh failed for %s: %s", tender_id, exc)
                continue
            summary["refreshed"] += 1 if res.get("ok") else 0
            summary["docs_captured"] += (res.get("downloaded") or {}).get("captured", 0)
    summary["requests"] = client.request_count - start_requests
    _p(f"  corrigendums: {rows_seen} rows / {pages} pages, {new_total} new tenders, "
       f"{summary['refreshed']} refreshed")
    return summary


class LatestWatch:
    """Wall-clock scheduler for the polls, ticked from inside the long passes.

    A poll that only ran between capture cycles would inherit the cycle's
    period, and a cycle is currently hours long (one org-tree walk plus the
    whole detail/document backlog it feeds). Ticking from inside those loops is
    what makes ``poll_interval_s`` an actual interval rather than a lower bound
    nobody reaches.

    ``tick`` is designed to be called in a tight loop: it is a clock comparison
    until it is due, and it never raises into its caller. The rolling hourly
    request ceiling is the runaway stop — ``max_requests_per_run`` is 0 in this
    deployment, so nothing else would bound a poll that started misbehaving.

    Consecutive failures double the interval up to ``max_backoff_s``, and a
    single success puts it straight back to ``poll_interval_s``. This is not
    just tidiness: the surface is captcha-gated and the failure it protects
    against was real — a run of rejections that looked like bad captchas and
    was in fact a stale-link rewind (see _search_form) — so the backoff is the
    thing that kept a broken poll from POSTing a captcha every five minutes for
    a day. Being told no by public infrastructure is a reason to ask less
    often, whatever the reason turns out to be.
    """

    def __init__(self, cfg, *, interval_s: float | None = None,
                 corrigendum_interval_s: float | None = None, progress=None):
        opts = settings(cfg)
        self.cfg = cfg
        self.opts = opts
        self.interval = float(interval_s if interval_s is not None
                              else opts["poll_interval_s"])
        self.corr_interval = float(
            corrigendum_interval_s if corrigendum_interval_s is not None
            else opts["corrigendum_interval_s"])
        self.progress = progress
        self.totals = {"polls": 0, "new": 0, "urgent": 0, "docs_captured": 0,
                       "corrigendum_sweeps": 0, "refreshed": 0, "requests": 0,
                       "throttled": 0, "failed": 0}
        self._next_due = 0.0
        # Deliberately not due immediately: the first corrigendum sweep is a
        # full walk and should not collide with process start-up.
        self._next_corr_due = time.monotonic() + self.corr_interval
        self._hour_start = time.monotonic()
        self._hour_requests = 0
        self._running = False
        self._consecutive_failures = 0
        self._max_backoff = float(opts["max_backoff_s"])

    def _reschedule(self, ok: bool) -> None:
        self._consecutive_failures = 0 if ok else self._consecutive_failures + 1
        delay = min(self.interval * (2 ** self._consecutive_failures),
                    self._max_backoff)
        if not ok and delay > self.interval:
            (self.progress or log.info)(
                f"  latest: {self._consecutive_failures} failed poll(s); "
                f"next attempt in {int(delay)}s")
        self._next_due = time.monotonic() + delay

    def _budget_ok(self, want: int) -> bool:
        now = time.monotonic()
        if now - self._hour_start >= 3600:
            self._hour_start = now
            self._hour_requests = 0
        return self._hour_requests + want <= int(self.opts["max_requests_per_hour"])

    def due(self) -> bool:
        return time.monotonic() >= self._next_due

    def tick(self, conn, client: HttpClient, *, force: bool = False) -> dict | None:
        """Run whichever poll is due. Returns its summary, or None if none was."""
        if self._running or not (force or self.due()):
            return None
        corr = time.monotonic() >= self._next_corr_due
        # A corrigendum sweep is the whole list; reserve accordingly.
        if not self._budget_ok(60 if corr else 6):
            self.totals["throttled"] += 1
            self._next_due = time.monotonic() + self.interval
            return None
        self._running = True
        before = client.request_count
        ok = False
        try:
            if corr:
                res = poll_corrigendums(conn, client, self.cfg,
                                        progress=self.progress)
                self._next_corr_due = time.monotonic() + self.corr_interval
                self.totals["corrigendum_sweeps"] += 1
                self.totals["refreshed"] += res.get("refreshed", 0)
            else:
                res = poll_latest(conn, client, self.cfg, progress=self.progress)
                self.totals["polls"] += 1
                self.totals["urgent"] += res.get("urgent", 0)
            self.totals["new"] += res.get("new", 0)
            self.totals["docs_captured"] += res.get("docs_captured", 0)
            ok = bool(res.get("ok"))
            if not ok:
                self.totals["failed"] += 1
            return res
        except Exception as exc:  # noqa: BLE001
            # The poll is an add-on to whatever pass is ticking it; it must
            # never be the reason that pass dies.
            log.warning("latest poll failed: %s", exc)
            self.totals["failed"] += 1
            return None
        finally:
            used = client.request_count - before
            self._hour_requests += used
            self.totals["requests"] += used
            self._reschedule(ok)
            self._running = False


def run_latest(db_path=None, *, max_pages: int | None = None, capture: bool = True,
               corrigendums: bool = False, progress=None) -> dict:
    """One-shot poll, for the CLI and for manual runs."""
    cfg = load_config()
    db_path = db_path or cfg.db_path
    cfg.ensure_dirs()
    init_db(db_path)
    conn = connect(db_path)
    client = HttpClient(cfg)
    try:
        fn = poll_corrigendums if corrigendums else poll_latest
        return fn(conn, client, cfg, max_pages=max_pages, capture=capture,
                  progress=progress or print)
    finally:
        conn.close()
