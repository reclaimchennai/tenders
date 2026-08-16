"""Enumerate historical tenders via the "Tenders Status" search.

Every other listing surface on this GePNIC deployment is forward-looking:
the organisation tree and "Tenders by Closing Date" only ever show tenders that
have not closed yet (the latter refuses a past date outright, server-side), and
"Cancelled/Retendered" and "Results of Tenders" are capped at 100 rows with no
pager. ``WebTenderStatusLists`` is the one surface that reaches backwards: its
"Search Criteria I" takes a *tender status* plus a ``fromDate``/``toDate``
range, and its result table pages without limit. That makes it the only way to
recover tenders that opened and closed while the archive was offline.

Shape of the search:
* Criteria I (status + date range + keyword/category filters) takes precedence
  over criteria II and III, so we must leave those blank or they are ignored
  anyway. ``tenderStatus`` is mandatory; the date range narrows what is
  otherwise a 780k-row corpus down to a walkable slice.
* ``fromDate``/``toDate`` filter on **bid submission closing date**, not
  publication date — verified by matching a month's results against tenders we
  already hold dates for. Coverage must therefore be planned along the closing
  axis: a tender published just before an outage but closing during it is found
  in the *closing* month's window, and nothing is missed as long as closing
  windows are contiguous. (Criteria II's ``publishedFromDate``/
  ``publishedToDate`` are silently ignored whenever a status is set.)
* Results are 10 rows per page, advanced by a session-bound ``loadNext`` GET —
  an offset cursor held in the session, not an addressable page number. So a
  window must be walked start-to-finish inside one session; there is no way to
  jump to page N.
* The listing carries Tender ID, title, reference number, organisation chain
  and stage — but **no dates**. Its row links point at a stage-summary page
  rather than at ``FrontEndViewTender``, which is why this surface was read as
  recovering only the *record* that a tender existed.

  That turns out to be half true. The row link's ``sp`` token is an opaque
  tender key, not a session key: rewritten to ``page=FrontEndViewTender`` and
  stripped of ``session=T`` it resolves, in a fresh session, to the tender's
  ordinary detail page — verified against a closed 2025 tender, dates and all.
  So the walk now stores a permalink per row at **zero extra requests**, and
  dating a register entry afterwards costs one ordinary detail fetch instead of
  being impossible. The documents themselves are still gone; what comes back is
  the full metadata record (dates, value, EMD, category, work description,
  document names and sizes, corrigenda).

Because the cursor cannot be addressed, an interrupted window restarts from its
first page on the next run rather than resuming mid-walk. Re-reading rows is
free of consequence (inserts are ON CONFLICT) and costs only the requests it
would have cost to fast-forward the cursor anyway.
"""

from __future__ import annotations

import calendar
import html as htmllib
import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from .captcha import solve_image
from .config import load_config
from .db import connect, init_db
from .http_client import HttpClient, RequestCapExceeded
from .jsf import extract_form
from .parse_listing import permalink, sp_token
from .util import now_iso

log = logging.getLogger("status")

_PAGE = "WebTenderStatusLists"
_FORM = "frmSearchFilter"
_TENDER_ID = re.compile(r"\d{4}_[A-Za-z]+_\d+_\d+")
_TOTAL = re.compile(r"Total records:\s*(\d+)")
_LOAD_NEXT = re.compile(r'id="loadNext"[^>]*href="([^"]+)"')
# "[Title][Reference number]" packed into one cell.
_TITLE_REF = re.compile(r"^\[(.*)\]\s*\[([^\]]*)\]\s*$", re.DOTALL)

# Portal's tenderStatus dropdown. Every tender sits in exactly one of these, so
# walking all nine over a date range covers that range completely.
STATUSES = {
    "1": "To Be Opened",
    "2": "Technical Bid Opening",
    "3": "Technical Evaluation",
    "4": "Financial Bid Opening",
    "5": "Financial Evaluation",
    "6": "AOC",
    "7": "Retender",
    "8": "Cancelled",
    "9": "Concluded",
}

# Stages that say something about the tender beyond "it exists", and that the
# schema has somewhere to put.
_CANCELLED_STAGES = {"Cancelled", "Retender"}


def parse_status_rows(html_text: str, host: str = "") -> list[dict]:
    """Return [{tender_id, title, reference_number, organisation_chain, stage,
    detail_url}]."""
    soup = BeautifulSoup(html_text, "lxml")
    table = soup.find("table", id="tabList")
    if table is None:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        tid = re.sub(r"\s+", "", tds[1].get_text())
        if not _TENDER_ID.fullmatch(tid) or tid in seen:
            continue
        seen.add(tid)
        cell = re.sub(r"\s+", " ", tds[2].get_text(" ", strip=True)).strip()
        m = _TITLE_REF.match(cell)
        title, ref = (m.group(1).strip(), m.group(2).strip()) if m else (cell, None)
        a = tr.find("a", href=True)
        out.append({
            "tender_id": tid,
            "title": htmllib.unescape(title) or None,
            "reference_number": htmllib.unescape(ref) if ref else None,
            "organisation_chain": re.sub(
                r"\s+", " ", tds[3].get_text(" ", strip=True)).strip() or None,
            "stage": re.sub(r"\s+", " ", tds[4].get_text(" ", strip=True)).strip() or None,
            "detail_url": permalink(a["href"], host) if a else None,
        })
    return out


def _search(client: HttpClient, cfg, status: str, from_date: str, to_date: str,
            *, attempts: int = 6, progress=None) -> str | None:
    """Solve the captcha and POST criteria I; return the first result page.

    A rejected captcha re-renders the bare search form, which never contains
    the ``tabList`` result table — so the table's presence is the success test.
    Looking for "Invalid Captcha!" would not work: the page carries that string
    in a JS validation template whatever the outcome.
    """
    _p = progress or (lambda m: log.info(m))
    url = cfg.host + f"/nicgep/app?page={_PAGE}&service=page"
    for i in range(1, attempts + 1):
        resp = client.get(url)
        form = extract_form(resp.text, _FORM)
        if not form or not form.get("captcha_src"):
            log.debug("no captcha form on status search attempt %d", i)
            continue
        # tracked=False: this walk never tells the solver whether an answer was
        # accepted, so every solve it makes would look "unconfirmed" forever and
        # a long backfill would trip the solver's circuit breaker on its own —
        # demoting the document downloads, which are the thing that breaker is
        # actually meant to protect.
        solution = solve_image(form["captcha_src"], tracked=False)
        if not solution:
            continue
        fields = dict(form["fields"])
        fields.update({
            "tenderStatus": status,
            "fromDate": from_date,
            "toDate": to_date,
            "captchaText": solution,
            "Search": "Search",
            "submitmode": "S",
            "submitname": "Search",
        })
        page = client.post(cfg.host + form["action"], data=fields)
        if 'id="tabList"' in page.text:
            return page.text
        _p(f"    status search attempt {i}: captcha rejected, retrying")
    _p(f"    status search failed after {attempts} attempts")
    return None


def _record(conn, rows: list[dict], stage_label: str) -> int:
    """Insert discovered tenders; return the count of genuinely new rows.

    Deliberately never writes ``tender_type`` — the portal's genuine type
    ("Open Tender", "Limited", …) comes from the detail page and a stage label
    is not a substitute for it.
    """
    ts = now_iso()
    new = 0
    for r in rows:
        stage = r.get("stage") or stage_label
        cancelled = ts if stage in _CANCELLED_STAGES else None
        # total_changes counts the ON CONFLICT update too, so it cannot tell a
        # genuinely new tender from a re-listed one — and "how many did this
        # surface add" is the whole point of running the backfill.
        if conn.execute("SELECT 1 FROM tenders WHERE tender_id = ?",
                        (r["tender_id"],)).fetchone() is None:
            new += 1
        conn.execute(
            """
            INSERT INTO tenders (tender_id, title, reference_number,
                organisation_chain, cancelled_at, cancellation_note,
                detail_url, sp_token,
                source, status, first_seen_at, last_updated_at)
            VALUES (:tid, :title, :ref, :org, :cancelled, :note, :url, :sp,
                    'scraped', 'discovered', :ts, :ts)
            ON CONFLICT(tender_id) DO UPDATE SET
                title = COALESCE(tenders.title, excluded.title),
                reference_number = COALESCE(tenders.reference_number, excluded.reference_number),
                organisation_chain = COALESCE(tenders.organisation_chain, excluded.organisation_chain),
                cancelled_at = COALESCE(tenders.cancelled_at, excluded.cancelled_at),
                cancellation_note = COALESCE(tenders.cancellation_note, excluded.cancellation_note),
                detail_url = COALESCE(tenders.detail_url, excluded.detail_url),
                sp_token = COALESCE(tenders.sp_token, excluded.sp_token),
                last_updated_at = excluded.last_updated_at
            """,
            {"tid": r["tender_id"], "title": r["title"], "ref": r["reference_number"],
             "org": r["organisation_chain"], "cancelled": cancelled,
             "note": stage if cancelled else None,
             "url": r.get("detail_url"), "sp": sp_token(r.get("detail_url")),
             "ts": ts},
        )
    return new


def _window_key(status: str, from_date: str, to_date: str) -> str:
    return f"status:{status}:{from_date}:{to_date}"


def enumerate_window(conn, client: HttpClient, cfg, status: str,
                     from_date: str, to_date: str, *, max_pages: int = 0,
                     progress=None) -> dict:
    """Walk one (status, date range) slice to exhaustion or ``max_pages``."""
    _p = progress or (lambda m: log.info(m))
    label = STATUSES.get(status, status)
    key = _window_key(status, from_date, to_date)

    done = conn.execute(
        "SELECT complete FROM crawl_state WHERE listing = ?", (key,)).fetchone()
    if done and done["complete"]:
        return {"status": label, "skipped": True, "rows": 0, "new": 0, "pages": 0}

    html_text = _search(client, cfg, status, from_date, to_date, progress=_p)
    if html_text is None:
        return {"status": label, "rows": 0, "new": 0, "pages": 0, "complete": False}

    m = _TOTAL.search(BeautifulSoup(html_text, "lxml").get_text(" ", strip=True))
    total = int(m.group(1)) if m else None
    _p(f"  {label} {from_date}..{to_date}: {total if total is not None else '?'} records")

    rows_seen = 0
    new_total = 0
    pages = 0
    complete = True
    while True:
        rows = parse_status_rows(html_text, cfg.host)
        pages += 1
        rows_seen += len(rows)
        new_total += _record(conn, rows, label)
        conn.execute(
            "INSERT INTO crawl_state (listing, page, complete, updated_at) "
            "VALUES (?, ?, 0, ?) ON CONFLICT(listing) DO UPDATE SET "
            "page = excluded.page, updated_at = excluded.updated_at",
            (key, pages, now_iso()),
        )
        conn.commit()
        if max_pages and pages >= max_pages:
            complete = False
            _p(f"    page cap ({max_pages}) reached for {label} {from_date}")
            break
        nxt = _LOAD_NEXT.search(html_text)
        if not nxt:
            break
        try:
            resp = client.get(cfg.host + htmllib.unescape(nxt.group(1)))
        except RequestCapExceeded:
            complete = False
            _p("    request cap reached mid-window")
            break
        if resp.status_code != 200:
            complete = False
            break
        html_text = resp.text

    if complete:
        conn.execute(
            "UPDATE crawl_state SET complete = 1, updated_at = ? WHERE listing = ?",
            (now_iso(), key),
        )
        conn.commit()
    _p(f"    {label} {from_date}: {rows_seen} rows over {pages} pages, {new_total} new")
    return {"status": label, "rows": rows_seen, "new": new_total, "pages": pages,
            "total": total, "complete": complete}


def month_windows(start: str, end: str) -> list[tuple[str, str]]:
    """Calendar-month closing-date (from, to) pairs in the portal's dd/MM/yyyy.

    Months are the natural slice: small enough that a single interrupted window
    is cheap to redo, and contiguous months leave no closing date uncovered.
    """
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    out = []
    y, mth = sy, sm
    while (y, mth) <= (ey, em):
        last = calendar.monthrange(y, mth)[1]
        out.append((f"01/{mth:02d}/{y}", f"{last:02d}/{mth:02d}/{y}"))
        mth += 1
        if mth == 13:
            y, mth = y + 1, 1
    return out


def run_status(db_path=None, *, statuses: list[str] | None = None,
               start: str | None = None, end: str | None = None,
               max_pages_per_window: int = 0, progress=None) -> dict:
    """Backfill discovered tenders for every (status, month) in the range."""
    _p = progress or (lambda m: log.info(m))
    cfg = load_config()
    db_path = db_path or cfg.db_path
    cfg.ensure_dirs()
    init_db(db_path)
    conn = connect(db_path)
    client = HttpClient(cfg)
    statuses = statuses or list(STATUSES)
    today = date.today()
    start = start or f"{today.year - 1}-{today.month:02d}"
    end = end or f"{today.year}-{today.month:02d}"

    summary = {"windows": 0, "rows": 0, "new": 0, "incomplete": []}
    try:
        for from_date, to_date in month_windows(start, end):
            for status in statuses:
                try:
                    res = enumerate_window(conn, client, cfg, status, from_date,
                                           to_date, max_pages=max_pages_per_window,
                                           progress=_p)
                except RequestCapExceeded:
                    _p("request cap reached; stopping status backfill")
                    return summary
                if res.get("skipped"):
                    continue
                summary["windows"] += 1
                summary["rows"] += res["rows"]
                summary["new"] += res["new"]
                if not res.get("complete"):
                    summary["incomplete"].append(
                        f"{STATUSES.get(status, status)} {from_date}")
    finally:
        conn.close()
    summary["requests"] = client.request_count
    return summary
