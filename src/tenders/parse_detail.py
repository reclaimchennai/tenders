"""Parse a GePNIC FrontEndViewTender detail page.

The page is a series of ``<td class="td_caption">Label</td>
<td class="td_field">Value</td>`` pairs plus document tables (class
``list_table``) identified by a "Document Name" header column.

Returns a dict with:
* ``fields``: every caption->value pair (the full record, for raw_json),
* ``documents``: list of dicts {filename, section, description, declared_size,
  document_type, download_url}. ``download_url`` is None when the file has been
  deleted from the portal (filename rendered as plain text, no anchor) — the
  lost-vs-captured signal.
* ``corrigenda``: the "Latest Corrigendum List" rows. This table sits outside
  the caption/field grid, so it was previously discarded wholesale — and it is
  the **only** place an ordinary detail page states that a tender was cancelled
  (``Corrigendum Type = 'Cancellation of Tender'``).
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

# Labels we promote to normalized columns. Maps label -> (column_base, is_date, is_money)
FIELD_MAP = {
    "Tender ID": ("tender_id", False, False),
    "Tender Reference Number": ("reference_number", False, False),
    "Title": ("title", False, False),
    "Work Description": ("work_description", False, False),
    "Organisation Chain": ("organisation_chain", False, False),
    "Tender Category": ("tender_category", False, False),
    "Tender Type": ("tender_type", False, False),
    "Product Category": ("product_category", False, False),
    "Location": ("location", False, False),
    "Pincode": ("pincode", False, False),
    "Tender Value in ₹": ("tender_value", False, True),
    "EMD Amount in ₹": ("emd", False, True),
    "Tender Fee in ₹": ("tender_fee", False, True),
    "Published Date": ("published_date", True, False),
    "Bid Opening Date": ("opening_date", True, False),
    "Bid Submission End Date": ("closing_date", True, False),
}

_WS = re.compile(r"\s+")


def _txt(node) -> str:
    return _WS.sub(" ", node.get_text(" ")).strip()


def _is_signature_link(a) -> bool:
    """True for links that are NOT individual file downloads.

    GePNIC decorates download cells with several icon links: a certificate icon
    (digital-signature popup), a print icon ("View More Details"), and a view
    icon. None of these stream the document, so we skip any icon link — EXCEPT
    the zip icon ("Download as zip file"), which is a real bulk download and is
    captured separately in ``_zip_documents``. A plain filename anchor (no icon)
    is the per-file download and is kept.
    """
    img = a.find("img")
    if img is not None:
        src = (img.get("src") or "").lower()
        if "zipicon" in src:
            return False  # the bulk-zip download — handled by _zip_documents
        return True  # certificate / print / view icon — not a file link
    href = (a.get("href") or "").lower()
    return "digitalsign" in href or "signature" in href


def _zip_documents(soup, base_url: str) -> list[dict]:
    """Capture every "Download as zip file" bulk-download link.

    Each tender document section offers a server-generated zip bundling all of
    that section's files (BOQ spreadsheets, technical docs, etc.). These are the
    only way to get work-item documents whose individual rows carry no direct
    link, so they are essential for a complete archive.
    """
    out: list[dict] = []
    seen_urls: set[str] = set()
    used_names: set[str] = set()
    for a in soup.find_all("a"):
        img = a.find("img")
        src = (img.get("src") or "").lower() if img else ""
        text = _txt(a).lower()
        if "zipicon" not in src and "download as zip" not in text:
            continue
        href = a.get("href")
        if not href:
            continue
        url = urljoin(base_url, href.replace("&amp;", "&"))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        cap = a.find_previous("td", class_="td_caption")
        section = _txt(cap).rstrip(":").strip() if cap else "Documents"
        slug = re.sub(r"[^A-Za-z0-9]+", "_", section).strip("_") or "documents"
        fname = f"{slug}.zip"
        n = 2
        while fname in used_names:
            fname = f"{slug}_{n}.zip"
            n += 1
        used_names.add(fname)
        out.append(
            {
                "filename": fname,
                "section": section,
                "document_type": "zip-bundle",
                "description": "All documents in this section (server-generated zip)",
                "declared_size": None,
                "download_url": url,
            }
        )
    return out


def _corrigenda(soup) -> list[dict]:
    """Rows of the "Latest Corrigendum List" table: S.No, Title, Type, View.

    The page ships a commented-out Tapestry template carrying the same table id,
    which the parser never sees (comments are not elements), so matching on the
    header text keeps this robust against the id changing.
    """
    out: list[dict] = []
    # Anchor on the header ROW, not the table: GePNIC nests its tables several
    # deep, so a descendant selector matches every wrapper above the real one
    # and would scrape the page navigation ("Cancelled/Retendered" menu link)
    # as if it were a corrigendum.
    for header in soup.select("tr.list_header"):
        headers = [_txt(td).lower() for td in header.find_all("td")]
        type_idx = next((i for i, h in enumerate(headers)
                         if "corrigendum type" in h), None)
        if type_idx is None:
            continue
        title_idx = next((i for i, h in enumerate(headers)
                          if "corrigendum title" in h), None)
        table = header.find_parent("table")
        if table is None:
            continue
        for tr in table.find_all("tr"):
            if tr is header or tr.find_parent("table") is not table:
                continue
            cells = [td for td in tr.find_all("td") if td.find_parent("tr") is tr]
            if len(cells) <= type_idx:
                continue
            ctype = _txt(cells[type_idx])
            if not ctype:
                continue
            out.append({
                "title": _txt(cells[title_idx]) if title_idx is not None
                         and len(cells) > title_idx else None,
                "type": ctype,
            })
    return out


def cancellation(corrigenda: list[dict]) -> dict | None:
    """The cancellation corrigendum, if the portal published one.

    Matches on the *type* column only. Free-text titles mentioning cancellation
    or withdrawal are not evidence — an archive that flags a tender cancelled on
    a typo in someone's corrigendum title is worse than one that misses it.
    """
    for c in corrigenda:
        if "cancel" in (c.get("type") or "").lower():
            return c
    return None


def _parse_doc_table(table, section: str, base_url: str) -> list[dict]:
    headers = [_txt(td).lower() for td in table.select("tr.list_header td")]
    if not headers:
        # Fall back to the first row as header.
        first = table.find("tr")
        headers = [_txt(td).lower() for td in first.find_all("td")] if first else []
    try:
        name_idx = next(i for i, h in enumerate(headers) if "document name" in h)
    except StopIteration:
        return []
    type_idx = next((i for i, h in enumerate(headers) if h == "document type"), None)
    desc_idx = next((i for i, h in enumerate(headers) if h == "description"), None)
    size_idx = next((i for i, h in enumerate(headers) if "size" in h), None)

    docs: list[dict] = []
    for tr in table.find_all("tr"):
        if "list_header" in (tr.get("class") or []):
            continue
        cells = tr.find_all("td", recursive=False)
        if not cells:
            cells = tr.find_all("td")
        if len(cells) <= name_idx:
            continue
        name_cell = cells[name_idx]
        filename = _txt(name_cell)
        if not filename:
            continue
        # The download link is an <a> that is NOT the signature popup.
        download_url = None
        for a in name_cell.find_all("a"):
            if _is_signature_link(a):
                continue
            href = a.get("href")
            if href:
                download_url = urljoin(base_url, href.replace("&amp;", "&"))
                break
        docs.append(
            {
                "filename": filename,
                "section": section,
                "document_type": _txt(cells[type_idx]) if type_idx is not None and len(cells) > type_idx else None,
                "description": _txt(cells[desc_idx]) if desc_idx is not None and len(cells) > desc_idx else None,
                "declared_size": _txt(cells[size_idx]) if size_idx is not None and len(cells) > size_idx else None,
                "download_url": download_url,
            }
        )
    return docs


def parse_detail(html: str, base_url: str = "https://tntenders.gov.in/nicgep/app") -> dict:
    soup = BeautifulSoup(html, "lxml")
    fields: dict[str, str] = {}
    documents: list[dict] = []

    for cap in soup.select("td.td_caption"):
        label = _txt(cap).rstrip(":").strip()
        if not label:
            continue
        field_td = cap.find_next_sibling("td")
        if field_td is None:
            continue
        # Is this a document section? (contains a table with a Document Name col)
        doc_table = None
        for tbl in field_td.find_all("table"):
            hdr = " ".join(_txt(td).lower() for td in tbl.select("tr.list_header td"))
            if "document name" in hdr:
                doc_table = tbl
                break
        if doc_table is not None:
            documents.extend(_parse_doc_table(doc_table, label, base_url))
            continue
        # Plain field. Skip ones that are really nested tables (covers, bankers).
        if field_td.find("table") is not None:
            continue
        value = _txt(field_td)
        # Avoid clobbering a real value with an empty one for duplicate labels.
        if label not in fields or (value and not fields[label]):
            fields[label] = value

    # Bulk "Download as zip file" links (the only source for many work-item docs).
    documents.extend(_zip_documents(soup, base_url))

    return {"fields": fields, "documents": documents,
            "corrigenda": _corrigenda(soup)}
