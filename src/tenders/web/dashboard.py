"""Front-page dashboard: fast SQL aggregates and the bar-chart series.

Everything here is cheap (indexed counts/sums). Time series are returned ready
to draw — see _chart, the template renders them, nothing ships to the client.
The 'short bidding window' watch surfaces a classic procurement-rigging signal:
tenders whose time between publication and bid-closing is only hours.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from ..redflags import short_window
from ..shortnames import headline
from .dates import fmt_date, fmt_datetime, ist as _ist, relative_short


def _fmt_inr(n: float | None) -> str:
    """Indian-style short currency: ₹1.2 Cr / ₹3.4 L / ₹500."""
    if not n:
        return "₹0"
    n = float(n)
    if n >= 1e7:
        return f"₹{n / 1e7:.2f} Cr"
    if n >= 1e5:
        return f"₹{n / 1e5:.2f} L"
    if n >= 1e3:
        return f"₹{n / 1e3:.0f}K"
    return f"₹{n:.0f}"


def _inr_exact(n: float) -> str:
    """Full rupee figure in Indian digit grouping (1,16,29,379.22).

    Western grouping beside the portal's own "1,19,00,000" reads as a different
    magnitude to anyone used to lakhs and crores, and this pair of numbers is
    read precisely to be compared.
    """
    whole, _, frac = f"{float(n):.2f}".partition(".")
    sign, whole = ("-", whole[1:]) if whole.startswith("-") else ("", whole)
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{whole}.{frac}"


def _series(conn, sql: str, start: date, days: int) -> list[tuple[str, int]]:
    """Run a (date,count) query and pad missing days with zero across the range."""
    got = {r[0]: r[1] for r in conn.execute(sql).fetchall()}
    out = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        out.append((d, got.get(d, 0)))
    return out


def _labels(dates: list[str], fmt: str) -> list[str]:
    out = []
    for d in dates:
        try:
            out.append(datetime.fromisoformat(d).strftime(fmt))
        except ValueError:
            out.append(d)
    return out


def _chart(series: list[tuple[str, int]]) -> dict:
    """Build a chart payload: short axis labels (DD Mon), full tooltip dates
    (mm-dd-yy), values, and a 'DD Mon - DD Mon' range caption.

    ``bars`` carries the geometry the template needs to draw the chart itself.
    Two bar charts did not justify 104 KB of Chart.js from a third-party CDN —
    the one external dependency on a site that otherwise ships nothing it does
    not host, and on a throttled phone 301 ms of main-thread time to draw
    fourteen rectangles. Percentage heights in flexbox are responsive for free
    and need no JavaScript at all.
    """
    dates = [d for d, _ in series]
    short = _labels(dates, "%d %b")
    values = [v for _, v in series]
    # Short, one-per-bar axis labels (day-of-month) keep ticks aligned under bars.
    labels = [a.lstrip("0") for a in _labels(dates, "%d")]
    full = _labels(dates, "%d-%m-%y")
    peak = max(values, default=0)
    return {
        "labels": labels,
        "full": full,
        "values": values,
        "range": f"{short[0]} – {short[-1]}" if short else "",
        "bars": [
            {"label": lab, "full": f, "value": v,
             "pct": round(100 * v / peak, 2) if peak else 0}
            for lab, f, v in zip(labels, full, values)
        ],
    }


def dashboard_data(conn) -> dict:
    today = date.today()

    def one(sql):
        r = conn.execute(sql).fetchone()
        return r[0] if r and r[0] is not None else 0

    value_today = one("SELECT SUM(tender_value_num) FROM tenders "
                      "WHERE date(published_date)=date('now')")
    value_active = one("SELECT SUM(tender_value_num) FROM tenders "
                       "WHERE closing_date>=datetime('now')")
    live = one("SELECT count(*) FROM tenders WHERE closing_date>=datetime('now')")
    exp_today = one("SELECT count(*) FROM tenders WHERE date(closing_date)=date('now') "
                    "AND closing_date>=datetime('now')")
    exp_tomorrow = one("SELECT count(*) FROM tenders "
                       "WHERE date(closing_date)=date('now','+1 day')")
    exp_7d = one("SELECT count(*) FROM tenders WHERE closing_date "
                 "BETWEEN datetime('now') AND datetime('now','+7 days')")
    pub_today = one("SELECT count(*) FROM tenders WHERE date(published_date)=date('now')")

    published = _series(
        conn,
        "SELECT date(published_date) d, count(*) n FROM tenders "
        "WHERE published_date >= date('now','-13 days') GROUP BY d",
        today - timedelta(days=13), 14)
    # Closing-soon: always the next 7 days (today .. +6).
    closing = _series(
        conn,
        "SELECT date(closing_date) d, count(*) n FROM tenders "
        "WHERE closing_date BETWEEN datetime('now') AND datetime('now','+6 days') GROUP BY d",
        today, 7)

    suspicious = _suspicious_rows(conn, "closing_date >= datetime('now','-2 days')",
                                  "hrs ASC", 12)

    return {
        "value_today": _fmt_inr(value_today), "pub_today": pub_today,
        "value_active": _fmt_inr(value_active), "live": live,
        "exp_today": exp_today, "exp_tomorrow": exp_tomorrow, "exp_7d": exp_7d,
        "published_chart": _chart(published),
        "closing_chart": _chart(closing),
        "suspicious": suspicious, "suspicious_count": len(suspicious),
    }


def tender_state(closing_date: str | None, published_date: str | None = None,
                 cancelled_at: str | None = None,
                 awarded_at: str | None = None) -> dict:
    """Live/Closed pill for a tender, computed IST-aware.

    The portal's dates are stored as naive IST wall-clock strings (see
    ``util.parse_date`` — dateutil with dayfirst=True, no tzinfo attached).
    Comparing them directly against SQLite's UTC ``datetime('now')`` (as the
    dashboard aggregate SQL does) silently mislabels tenders for up to 5.5
    hours around the deadline; localizing explicitly to Asia/Kolkata here
    avoids that.

    Precedence is decided by which facts can outlive each other, not by which
    is newest:

    * **Cancelled** outranks everything, award included. A cancelled tender's
      closing date still arrives on schedule, so without this check the archive
      would go on calling it "Live" right up to a deadline that no longer
      exists — and a tender awarded and *then* cancelled ends up cancelled.
    * **Awarded** outranks every date-derived state. The award is a signed
      document; the dates are a schedule. Where they disagree — an award
      recorded against a closing date still in the future, which the portal's
      own data does produce — the document is the stronger evidence, and
      "Live" would invite a bid on a contract that is already let.

    This ordering is what stops the page rendering an AWARDED seal beside a
    "Cancelled" pill, or vice versa.
    """
    if cancelled_at:
        return {"label": "Cancelled", "css": "st-cancelled"}
    if awarded_at:
        return {"label": "Awarded", "css": "st-awarded",
                "hint": "The department published an award-of-contract "
                        "document for this tender"}
    closes = _ist(closing_date)
    if closes is None:
        # 8,147 tenders recovered from the portal's status lists carry no dates
        # at all — that listing simply does not publish them. "Unknown" reads as
        # a fault in the archive; the record is complete, the dates were never
        # there to scrape.
        return {"label": "No dates", "css": "st-unknown",
                "hint": "The portal's listing for this tender carries no "
                        "publication or closing date"}
    now = datetime.now(timezone.utc)
    if closes < now:
        return {"label": "Closed", "css": "st-closed"}
    opens = _ist(published_date)
    if opens is not None and opens > now:
        # Its own class, not st-unknown: "Not yet open" is a scheduled state a
        # bidder can act on (amber — come back later), while "No dates" is an
        # absence of information (grey). Sharing a class painted them the same
        # and the ticker could not tell one from the other.
        return {"label": "Not yet open", "css": "st-pending"}
    if closes - now <= timedelta(hours=24):
        return {"label": "Closing today", "css": "st-soon"}
    return {"label": "Live", "css": "st-live"}


def award_panel(tender: dict, docs: list[dict] | None = None) -> dict | None:
    """Everything the tender page's award panel renders, or None if not awarded.

    The comparison between the department's estimate and the amount actually
    accepted is computed here rather than in the template because it is the one
    number on the page that must not be got wrong: a contract let at nine lakh
    against a five-lakh estimate is a finding, and a template expression that
    quietly divides by a missing estimate would turn it into a blank.
    """
    import json as _json

    if not (tender.get("awarded_at") or tender.get("award_stage")):
        return None
    if tender.get("cancelled_at"):
        # Cancellation outranks the award everywhere else (see tender_state);
        # the panel must not contradict the seal the page is already showing.
        return None

    try:
        bidders = _json.loads(tender.get("award_bidders_json") or "[]")
    except (ValueError, TypeError):
        bidders = []
    if not isinstance(bidders, list):
        bidders = []

    won = tender.get("award_value_num")
    est = tender.get("tender_value_num")
    delta = None
    if won and est:
        pct = (float(won) - float(est)) / float(est) * 100.0
        delta = {
            "pct": abs(pct),
            "direction": "above" if pct > 0 else "below",
            "css": "aw-over" if pct > 0 else "aw-under",
        }
    # Mark the winning row by amount, not by name. The comparative statement
    # writes the bidder as "RVS CONSTRUCTIONS(GSTN-33ABBFR0927P1Z0)" while the
    # document's own verdict line names plain "RVS CONSTRUCTIONS", so a string
    # comparison leaves the winning row unmarked in exactly the case that
    # matters most.
    for b in bidders:
        if isinstance(b, dict):
            b["is_winner"] = bool(
                won and b.get("amount") and abs(float(b["amount"]) - float(won)) < 0.005)
    # Literal rather than an import: the web package deliberately does not depend
    # on the scraper modules. It must stay in step with enrich_awards.AOC_SECTION.
    aoc_docs = [d for d in (docs or []) if d.get("section") == "Award of Contract"]
    return {
        "winner": tender.get("awarded_to"),
        "value": _fmt_inr(won) if won else None,
        "value_exact": _inr_exact(won) if won else None,
        "estimate": _fmt_inr(est) if est else None,
        "delta": delta,
        "date": fmt_date(tender.get("awarded_at")) or None,
        "signatory": tender.get("award_signatory"),
        "ref": tender.get("award_ref"),
        "bidders": bidders,
        "docs": aoc_docs,
    }


# How many candidate rows to pull per row actually wanted. The SQL below can
# only measure from published_date, so it over-selects (see _suspicious_rows);
# each row it hands back may be dropped by the real test in Python, and a
# LIMIT applied before that filter would silently return a short page. Ten was
# chosen against the live archive, where the SQL matched 550 tenders and the
# real test kept 548 — a factor of two would have been ample, and ten leaves
# room for the ratio to worsen without the page quietly truncating.
_SUSPICIOUS_OVERFETCH = 10


def _suspicious_rows(conn, when: str, order: str, limit: int) -> list[dict]:
    """Tenders whose *effective* bidding window was under 24 hours.

    The window cannot be computed in SQL. The portal's "Published Date" is an
    e-publishing timestamp that departments routinely enter late — one tender
    carried a Published Date eight days after its own bid submission window had
    opened — so ``closing_date - published_date`` measured a window no bidder
    ever experienced, and this page accused departments over it (see
    ``redflags.available_from`` for the full account). The dates that disprove
    it live inside ``raw_json`` in the portal's own ``05-Aug-2026 09:00 AM``
    format, which SQLite cannot parse, so the real test has to run in Python.

    The SQL is kept as a cheap pre-filter because it is a strict superset:
    ``available_from`` is the *minimum* of published_date and the availability
    fields, so the true window is always >= this one, and anything this clause
    rejects could never have passed the real test. It narrows ~96,000 tenders
    to a few hundred; ``redflags.short_window`` then makes the actual decision.
    """
    rows = conn.execute(
        f"""
        SELECT tender_id, title, short_name, organisation_chain, location,
               tender_value_num, tender_type, published_date, closing_date,
               cancelled_at, awarded_at, awarded_to, award_value_num,
               work_description, product_category, tender_category, raw_json,
               (julianday(closing_date)-julianday(published_date))*24 AS hrs
        FROM tenders
        WHERE published_date IS NOT NULL AND closing_date IS NOT NULL
          AND {when}
          AND (julianday(closing_date)-julianday(published_date)) > 0
          AND (julianday(closing_date)-julianday(published_date))*24 < 24
        ORDER BY {order} LIMIT ?
        """, (limit * _SUSPICIOUS_OVERFETCH,)).fetchall()

    out: list[dict] = []
    for r in rows:
        row = dict(r)
        try:
            raw = json.loads(row.pop("raw_json") or "{}")
        except (TypeError, ValueError):
            raw = {}
        hrs = short_window(row["published_date"], row["closing_date"],
                           raw if isinstance(raw, dict) else {})
        if hrs is None:
            continue
        # Show the window the bidder actually had, not the published_date one.
        row["hrs"] = hrs
        out.append(row)
        if len(out) >= limit:
            break
    return out


def suspicious_history(conn, limit: int = 100) -> list[dict]:
    """Past short-bidding-window tenders that have already closed."""
    return _suspicious_rows(conn, "closing_date < datetime('now')",
                            "closing_date DESC", limit)


# SQLite's datetime('now') is UTC; every date in this table is naive IST
# wall-clock (see tender_state). Comparing the two directly is a 5.5-hour error
# in exactly the place it hurts — around a deadline — so IST "now" is spelled out,
# the same way enrich_awards and forward_capture spell it. The aggregates above
# still compare against UTC and are still wrong by that margin; they are left
# for a change that can also fix the UTC ``date.today()`` the charts are padded
# against, since correcting one without the other only moves the seam.
_IST_NOW = "datetime('now', '+5 hours', '+30 minutes')"


def live_tenders(conn, limit: int = 40) -> list[dict]:
    """Currently-open tenders, most recently published first — the front-page board.

    Every row here is one a reader can still act on. Ordering by publication date
    alone put closed and not-yet-open tenders on a board headed RECENTLY
    PUBLISHED, which invites a bid on a tender that shut a week ago; the state
    pill said CLOSED beside it, which makes the board contradict itself rather
    than excuse it. So the window is bounded at both ends: published already,
    not yet closed, and neither cancelled nor awarded — an awarded contract is
    let whatever its schedule says (again, see tender_state's precedence).

    Headline, state and the two date strings are attached here rather than
    computed in the template because the marquee renders each row *twice* (the
    duplicated track is what makes the loop seamless), so a template-side call
    is paid 56 times per request for 28 distinct tenders. Done here it is paid
    28 times per DASHBOARD_TTL. The relative phrase is therefore up to 30
    seconds stale, which no deadline measured in days can notice.
    """
    rows = [dict(r) for r in conn.execute(
        f"""
        SELECT tender_id, title, short_name, organisation_chain, location,
               tender_value_num, tender_type, published_date, closing_date,
               cancelled_at, awarded_at, awarded_to, award_value_num,
               work_description, product_category, tender_category
        FROM tenders
        WHERE closing_date >= {_IST_NOW}
          AND published_date IS NOT NULL
          AND published_date <= {_IST_NOW}
          -- Unary + on the two null tests, and only there. Left alone the
          -- planner picks idx_tenders_awarded, whose "awarded_at IS NULL" arm
          -- is most of the archive, and then sorts what survives: 39 ms. The +
          -- leaves closing_date as the only indexable term, which bounds the
          -- scan at the ~5,300 tenders still open: 9 ms.
          AND +cancelled_at IS NULL
          AND +awarded_at IS NULL
        ORDER BY published_date DESC LIMIT ?
        """, (limit,)).fetchall()]
    for r in rows:
        r["state"] = tender_state(r["closing_date"], r["published_date"],
                                  r["cancelled_at"], r["awarded_at"])
        r["headline"] = r["short_name"] or headline(r)
        r["relative"] = relative_short(r["closing_date"])
        r["when"] = (fmt_datetime(r["closing_date"]) if r["closing_date"]
                     else "Published " + fmt_date(r["published_date"]))
    return rows
