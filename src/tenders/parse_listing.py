"""Parse GePNIC listing pages.

Two shapes appear under "Tenders by Organisation":
* an *organisation tree* (columns: S.No, Organisation Name, Tender Count) whose
  rows carry a drill-down ``DirectLink`` (session-bound) into that org, and
* a *tender list* (header contains "Title and Ref.No./Tender ID") whose rows
  carry a ``FrontEndViewTender`` link and a Tender ID.

A drilled page may contain either or both. We extract whatever is present so the
walker can recurse into sub-orgs and harvest tenders.

The tender-list shape is shared, column for column, with the "Latest Active
Tenders", "Latest Active Corrigendums" and "Tenders in Archive" result tables,
so ``parse_result_rows`` serves them all. Two details of that shape are easy to
get wrong and were, for a long time:

* the title cell is not a title. It is ``[Title] [Reference number][Tender ID]``
  in one cell, so taking the anchor text verbatim stores a title that begins
  with "[" and throws the reference number away.
* the row already carries the e-Published, closing and opening timestamps.
  Discovery used to drop them and wait for the detail page to supply them,
  which left every not-yet-detailed tender undateable — and therefore invisible
  to short-bid-window detection, which is the whole point of the archive.
"""

from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup

TENDER_ID_RE = re.compile(r"\d{4}_[A-Za-z]+_\d+_\d+")
# "[Title] [Reference number][Tender ID]". The title may itself contain
# brackets, so the trailing two groups are anchored at the end and the greedy
# title group gives back only as much as they need.
TITLE_REF_ID_RE = re.compile(
    r"^\[(.*)\]\s*\[([^\]]*)\]\s*\[(\d{4}_[A-Za-z]+_\d+_\d+)\]\s*$", re.DOTALL)
# Listings whose row links name their own page rather than the detail page.
# Enumerated rather than wildcarded so an organisation *drill* link can never be
# mistaken for a tender link and rewritten into a broken detail URL.
_VIEWABLE_PAGE = re.compile(
    r"page=(?:FrontEndLatestActiveTenders|FrontEndLatestActiveCorrigendums"
    r"|FrontEndTendersInArchive|WebTenderStatusLists)")


def _abs(href: str, host: str) -> str:
    href = html.unescape(href)
    return href if href.startswith("http") else host + href


def _strip_session(url: str) -> str:
    """Turn a session-bound view link into a stable permalink."""
    return url.replace("&session=T", "").replace("session=T&", "")


def permalink(href: str, host: str) -> str | None:
    """Stable ``FrontEndViewTender`` URL for a listing row's link, or None.

    The ``sp`` token is an opaque tender key, not a session key: the same token
    resolves in a fresh session once ``page=`` names the detail page and
    ``session=T`` is gone. That is what lets the "Latest Active …" listings —
    whose links name their own page — yield permalinks byte-identical to the
    organisation tree's without a follow-up request.
    """
    if not href:
        return None
    url = _strip_session(_abs(href, host))
    url = _VIEWABLE_PAGE.sub("page=FrontEndViewTender", url)
    # WebTenderStatusLists writes component=view, which the detail page does not
    # answer — that pair returns "Your session in the client area has expired"
    # however valid the sp token is. Rewriting page= alone minted 16,027 dead
    # permalinks before this was caught.
    url = url.replace("component=view&", "component=%24DirectLink&")
    if "FrontEndViewTender" not in url or "sp=" not in url:
        return None
    return url


def sp_token(url: str | None) -> str | None:
    from urllib.parse import parse_qs, urlparse

    if not url:
        return None
    return (parse_qs(urlparse(url).query).get("sp") or [None])[0]


def _header_index(cells: list[str]) -> dict[str, int] | None:
    """Map logical column -> position from a header row, or None if not one.

    Located by header text rather than by position because the sibling listings
    agree on the first six columns and disagree on the seventh (Tender Value vs
    Corrigendum), and one of them emits a stray trailing cell.
    """
    idx: dict[str, int] = {}
    for i, c in enumerate(cells):
        low = c.lower()
        if "published" in low:
            idx["published"] = i
        elif "closing" in low:
            idx["closing"] = i
        elif "opening" in low:
            idx["opening"] = i
        elif "title" in low and "ref" in low:
            idx["title"] = i
        elif "organisation" in low or "dept" in low:
            # "Tenders in Archive" labels the same column "Name of Dept./Orgn."
            idx["org"] = i
        elif "tender value" in low:
            idx["value"] = i
    return idx if "title" in idx else None


def parse_result_rows(table, host: str) -> list[dict]:
    """Rows of a GePNIC tender result table (organisation list or "Latest …")."""
    idx: dict[str, int] | None = None
    out: list[dict] = []
    seen: set[str] = set()
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        cells = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)) for td in tds]
        if idx is None:
            idx = _header_index(cells)
            if idx is not None:
                continue
            # Header not recognised (some drills omit it): use the layout every
            # one of these tables actually uses.
            idx = {"published": 1, "closing": 2, "opening": 3, "title": 4, "org": 5}

        def at(key: str) -> str | None:
            i = idx.get(key)
            if i is None or i >= len(cells):
                return None
            return cells[i].strip() or None

        cell = at("title") or ""
        m = TITLE_REF_ID_RE.match(cell)
        if m:
            title, ref, tid = m.group(1).strip(), m.group(2).strip(), m.group(3)
        else:
            found = TENDER_ID_RE.search(cell)
            if not found:
                continue
            title, ref, tid = cell, None, found.group(0)
        if tid in seen:
            continue
        seen.add(tid)

        a = tr.find("a", href=True)
        url = permalink(a["href"], host) if a else None
        out.append({
            "tender_id": tid,
            "title": html.unescape(title) or None,
            "reference_number": html.unescape(ref) if ref else None,
            "organisation_chain": at("org"),
            "published_raw": at("published"),
            "closing_raw": at("closing"),
            "opening_raw": at("opening"),
            "value_raw": at("value"),
            "detail_url": url,
        })
    return out


def parse_org_tree(html_text: str, host: str) -> list[dict]:
    """Return [{name, count, drill_url}] for organisation rows, if present."""
    soup = BeautifulSoup(html_text, "lxml")
    orgs: list[dict] = []
    for tbl in soup.find_all("table", class_="list_table"):
        if "Tender Count" not in tbl.get_text():
            continue
        for tr in tbl.find_all("tr"):
            tds = tr.find_all("td")
            a = tr.find("a", href=re.compile(r"FrontEndTendersByOrganisation"))
            if a and len(tds) >= 3:
                name = re.sub(r"\s+", " ", tds[1].get_text()).strip()
                count = re.sub(r"\s+", " ", tds[2].get_text()).strip()
                if name:
                    orgs.append({
                        "name": name,
                        "count": count,
                        "drill_url": _abs(a["href"], host),
                    })
    return orgs


def parse_tender_list(html_text: str, host: str) -> list[dict]:
    """Return the tender rows of an organisation drill-down page, if present."""
    soup = BeautifulSoup(html_text, "lxml")
    out: list[dict] = []
    seen: set[str] = set()
    for tbl in soup.find_all("table", class_="list_table"):
        if "Title and Ref" not in tbl.get_text():
            continue
        for row in parse_result_rows(tbl, host):
            # Only rows that actually link to a detail page are tenders we can
            # follow; the table is also used for headers and spacer rows.
            if not row["detail_url"] or row["tender_id"] in seen:
                continue
            seen.add(row["tender_id"])
            out.append(row)
    return out
