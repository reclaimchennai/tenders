"""Seed the mirror from the user's existing CSV export.

The CSV holds ~3,219 July-2025 tenders with full metadata but no usable document
download URLs (the document columns are concatenated blobs of
filename+description+size). Those documents are already deleted from the portal,
so every document recovered here is recorded with status='lost'.

Tender rows are inserted with ``ON CONFLICT DO NOTHING`` keyed on Tender ID, so a
later live re-scrape can coexist; scraped data is considered authoritative and
will update rows via the detail pipeline, not here.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import load_config
from .db import connect, init_db
from .util import clean_ws, now_iso, parse_date, parse_money

log = logging.getLogger("csv_import")

# Allow very large quoted fields (work descriptions can be huge).
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

FILENAME_RE = re.compile(
    r"[\w\-.()&,]+?\.(?:pdf|xls|xlsx|doc|docx|zip|rar|jpe?g|png|rtf|txt|csv)",
    re.IGNORECASE,
)

# CSV column -> normalized tender column.
DOC_COLUMNS = {
    "NIT Document": "NIT",
    "Work Item Documents": "Work Item",
    "PreBid Meeting Document": "Pre-Bid",
    "Letter of Acceptance / Letter of Intent": "Letter of Acceptance",
}


def extract_sp_token(link: str | None) -> str | None:
    if not link:
        return None
    try:
        qs = parse_qs(urlparse(link).query)
    except ValueError:
        return None
    vals = qs.get("sp")
    return vals[0] if vals else None


def recover_filenames(blob: str | None) -> list[str]:
    """Best-effort extraction of filenames from a concatenated CSV doc blob."""
    if not blob:
        return []
    seen: list[str] = []
    for m in FILENAME_RE.finditer(blob):
        name = m.group(0).strip(" .,")
        if name and name not in seen:
            seen.append(name)
    return seen


def _first(row: dict, *keys: str) -> str | None:
    """Return the first non-empty value among the given column names."""
    for k in keys:
        if k in row and row[k] and row[k].strip():
            return row[k].strip()
    return None


def import_csv(csv_path: Path, db_path: Path | None = None) -> dict:
    cfg = load_config()
    db_path = db_path or cfg.db_path
    init_db(db_path)
    conn = connect(db_path)

    inserted = 0
    skipped = 0
    docs_recorded = 0
    ts = now_iso()

    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                tender_id = _first(row, "Tender ID")
                if not tender_id:
                    skipped += 1
                    continue
                link = _first(row, "Link")
                value_raw = _first(row, "Tender Value in ₹", "Tender Value in Rs.")
                rec = {
                    "tender_id": tender_id,
                    "reference_number": _first(row, "Tender Reference Number"),
                    "title": clean_ws(_first(row, "Title")),
                    "work_description": clean_ws(_first(row, "Work Description")),
                    "organisation_chain": clean_ws(_first(row, "Organisation Chain")),
                    "tender_category": _first(row, "Tender Category"),
                    "tender_type": _first(row, "Tender Type"),
                    "product_category": _first(row, "Product Category"),
                    "location": clean_ws(_first(row, "Location")),
                    "pincode": _first(row, "Pincode"),
                    "tender_value_raw": value_raw,
                    "tender_value_num": parse_money(value_raw),
                    "emd_raw": _first(row, "EMD Amount in ₹"),
                    "tender_fee_raw": _first(row, "Tender Fee in ₹"),
                    "published_date_raw": _first(row, "Published Date", "e-Published Date"),
                    "published_date": parse_date(_first(row, "Published Date", "e-Published Date")),
                    "opening_date_raw": _first(row, "Bid Opening Date", "Opening Date"),
                    "opening_date": parse_date(_first(row, "Bid Opening Date", "Opening Date")),
                    "closing_date_raw": _first(row, "Bid Submission End Date", "Closing Date"),
                    "closing_date": parse_date(_first(row, "Bid Submission End Date", "Closing Date")),
                    "detail_url": link,
                    "sp_token": extract_sp_token(link),
                    "source": "csv",
                    "status": "detailed",
                    "raw_json": json.dumps(row, ensure_ascii=False),
                    "first_seen_at": ts,
                    "last_updated_at": ts,
                }
                cur = conn.execute(
                    """
                    INSERT INTO tenders (
                        tender_id, reference_number, title, work_description,
                        organisation_chain, tender_category, tender_type,
                        product_category, location, pincode, tender_value_raw,
                        tender_value_num, emd_raw, tender_fee_raw,
                        published_date, published_date_raw, opening_date,
                        opening_date_raw, closing_date, closing_date_raw,
                        detail_url, sp_token, source, status, raw_json,
                        first_seen_at, last_updated_at
                    ) VALUES (
                        :tender_id, :reference_number, :title, :work_description,
                        :organisation_chain, :tender_category, :tender_type,
                        :product_category, :location, :pincode, :tender_value_raw,
                        :tender_value_num, :emd_raw, :tender_fee_raw,
                        :published_date, :published_date_raw, :opening_date,
                        :opening_date_raw, :closing_date, :closing_date_raw,
                        :detail_url, :sp_token, :source, :status, :raw_json,
                        :first_seen_at, :last_updated_at
                    )
                    ON CONFLICT(tender_id) DO NOTHING
                    """,
                    rec,
                )
                if cur.rowcount == 0:
                    skipped += 1
                    continue
                inserted += 1

                # Recover document filenames from the concatenated blobs.
                for col, section in DOC_COLUMNS.items():
                    blob = row.get(col)
                    for name in recover_filenames(blob):
                        conn.execute(
                            """
                            INSERT INTO documents
                                (tender_id, filename, section, description,
                                 status, source)
                            VALUES (?, ?, ?, ?, 'lost', 'csv')
                            ON CONFLICT(tender_id, filename, section) DO NOTHING
                            """,
                            (tender_id, name, section, "recovered from CSV export"),
                        )
                        docs_recorded += 1

                if inserted % 500 == 0:
                    conn.commit()
                    log.info("imported %d tenders...", inserted)
        conn.commit()
    finally:
        conn.close()

    result = {"inserted": inserted, "skipped": skipped, "docs_recorded": docs_recorded}
    log.info("CSV import done: %s", result)
    return result
