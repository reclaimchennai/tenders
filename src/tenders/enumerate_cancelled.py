"""Enumerate Cancelled and Retendered tenders.

Unlike the active "Tenders by Organisation" tree (captcha-free GET drilling),
the Cancelled/Retendered list lives behind a **captcha-gated JSF search**
(``WebCancelledTenderLists``): pick a status, solve the captcha, POST. The
result table's detail links are **session-bound** ``$DirectLink`` URLs — they
only work inside the same session that ran the search — so we fetch each
tender's detail page immediately, in the same session, while the captcha unlock
is still valid.

The search captcha is the same image challenge as the document gate, so the
existing solver applies. The page echoes an "Invalid Captcha!" string inside a
JS validation template even on success, so success is judged by whether result
rows actually came back, not by absence of that string.

Cancelled tenders are recorded with their visible metadata first (so they are
searchable even if the detail fetch fails), then enriched + their surviving
documents captured via the normal detail pipeline.
"""

from __future__ import annotations

import html as htmllib
import logging
import re

from .captcha import solve_image
from .config import load_config
from .db import connect, init_db
from .http_client import HttpClient, RequestCapExceeded
from .jsf import extract_form
from .util import now_iso, parse_date

log = logging.getLogger("cancelled")

_PAGE = "WebCancelledTenderLists"
_STATUS = {"retender": "1", "cancelled": "2"}
_TENDER_ID = re.compile(r"\d{4}_[A-Za-z]+_\d+_\d+")
# One result row: a session-bound DirectLink into the cancelled list whose anchor
# text is the bracketed title, followed by "[ref no][tender id]". Row anchors use
# varying ids (DirectLink, DirectLink_0, DirectLink_1, …) so we key off the href.
_ROW = re.compile(
    r'href="([^"]*component=%24DirectLink[^"]*WebCancelledTenderLists[^"]*)"[^>]*>\s*'
    r'\[?(.*?)\]?\s*</a>\s*\[([^\]]*)\]\s*\[(\d{4}_[A-Za-z]+_\d+_\d+)\]'
    r'(.*?)</td>',
    re.IGNORECASE | re.DOTALL,
)
_DATE = re.compile(r"\d{2}-[A-Za-z]{3}-\d{4}(?:\s+\d{2}:\d{2}\s*[AP]M)?")


def _search(client: HttpClient, cfg, status_value: str, *,
            attempts: int = 12, progress=None) -> str | None:
    """Solve the captcha and POST the status search; return result HTML or None.

    Success is detected by the presence of tender-id rows in the response, since
    the page contains a misleading "Invalid Captcha!" string in a JS template
    regardless of outcome.
    """
    _p = progress or (lambda m: log.info(m))
    url = cfg.host + f"/nicgep/app?page={_PAGE}&service=page"
    for i in range(1, attempts + 1):
        page = client.get(url)
        form = extract_form(page.text, _PAGE)
        if not form or not form.get("captcha_src"):
            log.debug("no captcha form on cancelled search attempt %d", i)
            continue
        # tracked=False: nothing here reports an accepted solve back, so these
        # would accumulate as "unconfirmed" and trip the solver's breaker for
        # the whole process. See captcha.solve_image.
        solution = solve_image(form["captcha_src"], tracked=False)
        if not solution:
            continue
        fields = dict(form["fields"])
        fields["tenderStatus"] = status_value
        fields["captchaText"] = solution
        fields["Search"] = "Search"
        fields["submitmode"] = "S"
        fields["submitname"] = "Search"
        resp = client.post(cfg.host + form["action"], data=fields)
        if _TENDER_ID.search(resp.text):
            n = len(set(_TENDER_ID.findall(resp.text)))
            _p(f"    cancelled search ok (attempt {i}): {n} rows")
            return resp.text
        _p(f"    cancelled search attempt {i}: captcha rejected, retrying")
    _p(f"    cancelled search failed after {attempts} attempts")
    return None


def parse_cancelled_rows(html_text: str, host: str) -> list[dict]:
    """Extract [{tender_id, title, detail_url, published_date, closing_date,
    opening_date, organisation_chain}] from a result page.

    ``detail_url`` is the session-bound DirectLink — usable only in the session
    that produced this page.
    """
    # Isolate the result table to avoid matching navigation DirectLinks.
    start = html_text.find('id="tenderView"')
    body = html_text[start:] if start != -1 else html_text
    out: list[dict] = []
    seen: set[str] = set()
    for m in _ROW.finditer(body):
        href, title, _ref, tid, tail = m.groups()
        if tid in seen:
            continue
        seen.add(tid)
        # The three dates (e-Published, Bid Closing, Tender Opening) appear in the
        # row's preceding cells; grab the last three before the anchor.
        pre = body[max(0, m.start() - 700):m.start()]
        dates = _DATE.findall(pre)[-3:]
        published = dates[0] if len(dates) >= 1 else None
        closing = dates[1] if len(dates) >= 2 else None
        opening = dates[2] if len(dates) >= 3 else None
        # Organisation chain is the next cell after the tender-id cell (|| sep).
        org = None
        org_m = re.search(r"<td[^>]*>(.*?)</td>", tail + body[m.end():m.end() + 400],
                          re.DOTALL)
        if org_m:
            org = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", org_m.group(1))).strip("| ").strip()
        out.append({
            "tender_id": tid,
            "title": re.sub(r"\s+", " ", htmllib.unescape(title)).strip(),
            "detail_url": host + htmllib.unescape(href),
            "published_date": published,
            "closing_date": closing,
            "opening_date": opening,
            "organisation_chain": org or None,
        })
    return out


def _upsert_metadata(conn, row: dict, status_label: str) -> None:
    """Record the visible metadata so the tender is searchable even if the
    detail fetch later fails. Never downgrades an already-detailed tender.

    ``tender_type`` is left alone: it holds the portal's procurement type
    ("Open Tender", "Limited", …) from the detail page, and writing
    "Cancelled"/"Retender" over it destroys that. The cancellation lives in the
    first-class ``cancelled_at``/``cancellation_note`` columns instead.
    """
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO tenders (tender_id, title, organisation_chain,
            published_date, published_date_raw, closing_date, closing_date_raw,
            opening_date, opening_date_raw, cancelled_at, cancellation_note,
            source, status, first_seen_at, last_updated_at)
        VALUES (:tid, :title, :org, :pub, :pub_raw, :close, :close_raw,
            :open, :open_raw, :ts, :note, 'scraped', 'discovered', :ts, :ts)
        ON CONFLICT(tender_id) DO UPDATE SET
            title = COALESCE(tenders.title, excluded.title),
            organisation_chain = COALESCE(tenders.organisation_chain, excluded.organisation_chain),
            cancelled_at = COALESCE(tenders.cancelled_at, excluded.cancelled_at),
            cancellation_note = COALESCE(tenders.cancellation_note, excluded.cancellation_note),
            last_updated_at = excluded.last_updated_at
        """,
        {
            "tid": row["tender_id"], "title": row["title"],
            "org": row["organisation_chain"],
            "pub": parse_date(row["published_date"]), "pub_raw": row["published_date"],
            "close": parse_date(row["closing_date"]), "close_raw": row["closing_date"],
            "open": parse_date(row["opening_date"]), "open_raw": row["opening_date"],
            "note": status_label, "ts": ts,
        },
    )


def enumerate_cancelled(conn, client: HttpClient, cfg, *, download: bool = True,
                        progress=None) -> dict:
    """Search Cancelled + Retender, record metadata, and fetch each detail page
    (and documents) in-session via its session-bound DirectLink."""
    _p = progress or (lambda m: log.info(m))
    from .pipeline import fetch_and_store_detail

    summary = {"found": 0, "detailed": 0, "docs_captured": 0}
    for label, value in (("Cancelled", _STATUS["cancelled"]),
                         ("Retender", _STATUS["retender"])):
        _p(f"  enumerating {label} tenders…")
        try:
            html_text = _search(client, cfg, value, progress=_p)
        except RequestCapExceeded:
            _p("  request cap hit during cancelled search")
            break
        if not html_text:
            continue
        rows = parse_cancelled_rows(html_text, cfg.host)
        summary["found"] += len(rows)
        _p(f"  {label}: {len(rows)} tenders")
        for row in rows:
            _upsert_metadata(conn, row, label)
            conn.commit()
            # The DirectLink only works in this session; resolve it now.
            already = conn.execute(
                "SELECT status FROM tenders WHERE tender_id=?",
                (row["tender_id"],),
            ).fetchone()
            if already and already["status"] == "detailed":
                continue
            try:
                res = fetch_and_store_detail(
                    conn, client, cfg, row["tender_id"], row["detail_url"],
                    download=download, progress=_p)
                if res.get("ok"):
                    summary["detailed"] += 1
                summary["docs_captured"] += (res.get("downloaded") or {}).get("captured", 0)
            except RequestCapExceeded:
                _p("  request cap hit during cancelled detail fetch")
                return summary
            except Exception as exc:  # noqa: BLE001
                log.warning("cancelled detail failed for %s: %s",
                            row["tender_id"], exc)
    return summary


def run_cancelled(db_path=None, *, download: bool = True) -> dict:
    cfg = load_config()
    db_path = db_path or cfg.db_path
    cfg.ensure_dirs()
    init_db(db_path)
    conn = connect(db_path)
    client = HttpClient(cfg)
    try:
        result = enumerate_cancelled(conn, client, cfg, download=download)
    finally:
        conn.close()
    result["requests"] = client.request_count
    return result
