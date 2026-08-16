"""Recover Award-of-Contract data: who actually won, for how much, and the proof.

The ordinary ``FrontEndViewTender`` detail page says nothing about the award —
not the winner, not the accepted amount, not the date — no matter what stage the
tender has reached. Verified against tenders the portal itself files under
"AOC": the page ends at the Tender Inviting Authority and there is no award
section, hidden table or otherwise. So award data cannot come from the pipeline
that fills every other column.

It comes from one place: the **AOC document** the department uploads, which the
portal exposes under ``ResultOfTenders``. Two things make it cheap to collect:

* the ``sp`` token is an opaque *tender* key, not a session key, and the
  ``component``/``page`` pair alone decides which view of that tender is
  rendered. ``component=$DirectLink_1&page=ResultOfTenders`` with the very token
  the status-list walk already stores therefore streams the AOC document
  directly — **no captcha, no session, one request per tender**. The
  captcha-gated ``ResultOfTenders`` search is not needed at all;
* a tender with no award answers the same URL with the ordinary search page
  carrying "No Results Of Tenders found", so a miss is as cheap as a hit and is
  unambiguous.

What comes back is one of two documents, and the difference matters:

* the portal's own generated **"BOQ Summary Details"** — a comparative statement
  listing *every* bidder with their amount and rank (L1, L2, …). This is the
  structured case, and the only case we populate ``awarded_to`` from;
* a department's own **award letter**, in whatever shape they wrote it.

Free text is not parsed for a winner. Naming the wrong company as the recipient
of a public contract is a far worse failure than naming none, so an unrecognised
document is stored, indexed and dated, and the structured columns stay NULL.

Every AOC document is digitally signed, and the signature's ``/M`` timestamp has
matched the portal's own "AOC Date" exactly wherever both were observed — so the
award date and the signing officer come out of the PDF itself, at no extra
request.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from urllib.parse import quote

from .config import load_config
from .db import connect, init_db
from .download_docs import _disposition_name, safe_filename, store_document
from .http_client import HttpClient, RequestCapExceeded
from .util import now_iso, parse_money

log = logging.getLogger("awards")

AOC_SECTION = "Award of Contract"

# Rows of the generated "BOQ Summary Details" statement, e.g.
#   BoQ1   1 C Dhanapal (BID ID -1481671)   395305.50 L1
#          2 T.Thirumalai (BID ID -1481676) 407572.24 L2
# The optional leading sheet name only appears on a sheet's first row, and the
# amount/rank pair at the end is what actually anchors the match. Separators are
# ``\s*`` rather than ``\s+`` because the PDFs are laid out in table cells, not
# words: where a column is narrow the text layer runs the cells together
# ("1P RAYIN 19081458.28L1") and requiring a space silently drops the row.
_BOQ_ROW = re.compile(
    r"^(?:(\S+)\s+)?(\d{1,3})\s*(.+?)\s*([\d,]+\.\d{1,2})\s*(L\d+)\s*$")
_BOQ_HEADER = re.compile(r"Sheet\s*Name\s+Sl\.?No\s+Bidder\s*Name", re.I)
_PDF_TENDER_ID = re.compile(r"Tender\s*ID:\s*(\d{4}_[A-Za-z]+_\d+_\d+)")

# The other generated form, headed "SCHEDULE OF WORK / ITEM(S)". Unlike the BOQ
# summary it does not rank the bidders, but it closes with the portal's own
# one-line verdict — which is a far safer thing to read than the table:
#   Lowest Amount Quoted BY: RVS CONSTRUCTIONS(11629379.22)
_LOWEST = re.compile(
    r"Lowest\s+Amount\s+Quoted\s+BY\s*:\s*(.+?)\s*\(\s*([\d,]+\.?\d*)\s*\)", re.I)
_SCHEDULE_HEADER = re.compile(r"SCHEDULE\s+OF\s+WORK\s*/\s*ITEM", re.I)
# Its bidder rows: "1.00RVS CONSTRUCTIONS(GSTN-…) 11854616.95 -1.90 11629379.22…"
# — serial number glued to the name, then estimate, quoted percentage, quoted
# amount. The percentage is the interesting column: it is how far above or below
# the department's own estimate the bid was.
_SCHEDULE_ROW = re.compile(
    r"^(\d{1,3})\.00(.+?)\s+([\d,]+\.\d{2})\s+(-?[\d.]+)\s+([\d,]+\.\d{2})")
# Departments fill this in with the real contract/work-order number. The class
# after the colon is [ \t] and not \s: the field is frequently left blank, and
# \s would step over the newline and capture the next heading as the number.
_CONTRACT_NO = re.compile(r"Contract\s*No\s*:[ \t]*(\S[^\n]*)")
# PDF signature dictionary entries. Read from the raw bytes rather than through a
# PDF library: these files carry embedded fonts that make strict parsers throw,
# and the two values we want are plain literal strings either way.
_SIG_DATE = re.compile(rb"/M\s*\(D:(\d{14})")
_SIG_NAME = re.compile(rb"/Name\s*\(([^)]{2,80})\)")


def aoc_url(host: str, sp_token: str) -> str:
    """The AOC document endpoint for a tender's ``sp`` token."""
    return (f"{host}/nicgep/app?component=%24DirectLink_1&page=ResultOfTenders"
            f"&service=direct&sp={quote(sp_token, safe='')}")


def signature(pdf: bytes) -> tuple[str | None, str | None]:
    """(ISO signing timestamp, signer name) from the PDF's signature dictionary.

    The timestamp is IST wall-clock, matching how every other date in this
    archive is stored (see util.parse_date), so the ``+05'30'`` suffix the file
    carries is dropped rather than converted.
    """
    dates = sorted(m.group(1).decode() for m in _SIG_DATE.finditer(pdf))
    stamp = None
    if dates:
        d = dates[-1]
        stamp = f"{d[0:4]}-{d[4:6]}-{d[6:8]}T{d[8:10]}:{d[10:12]}:{d[12:14]}"
    name = _SIG_NAME.search(pdf)
    signer = None
    if name:
        try:
            signer = name.group(1).decode("utf-8", "replace").strip() or None
        except Exception:  # noqa: BLE001 - a mangled name is simply no name
            signer = None
    return stamp, signer


def parse_boq_summary(text: str) -> list[dict]:
    """Bidders from a generated "BOQ Summary Details" statement, in file order."""
    if not text or not _BOQ_HEADER.search(text):
        return []
    out: list[dict] = []
    sheet = None
    for line in text.splitlines():
        line = line.strip()
        if not line or _BOQ_HEADER.search(line):
            continue
        m = _BOQ_ROW.match(line)
        if not m:
            continue
        if m.group(1):
            sheet = m.group(1)
        amount = parse_money(m.group(4))
        if amount is None:
            continue
        out.append({"sheet": sheet, "bidder": m.group(3).strip(),
                    "amount": amount, "rank": m.group(5).upper()})
    return out


def parse_schedule(text: str) -> list[dict]:
    """Bidders from a generated "SCHEDULE OF WORK / ITEM(S)" statement."""
    if not text or not _SCHEDULE_HEADER.search(text):
        return []
    out: list[dict] = []
    for line in text.splitlines():
        m = _SCHEDULE_ROW.match(line.strip())
        if not m:
            continue
        amount = parse_money(m.group(5))
        if amount is None:
            continue
        try:
            percent = float(m.group(4))
        except ValueError:
            percent = None
        out.append({"sheet": None, "bidder": m.group(2).strip(),
                    "amount": amount, "rank": None,
                    "estimated": parse_money(m.group(3)), "percent": percent})
    return out


def parse_award(text: str) -> dict:
    """Structured award facts from an AOC document, or empty for free text.

    Two generated forms are recognised; a department's own award letter is not
    parsed at all. Naming the wrong company as the recipient of a public
    contract is a far worse failure than naming none, so anything that is not
    one of the portal's own layouts yields no winner.
    """
    bidders = parse_boq_summary(text) or parse_schedule(text)
    out: dict = {"bidders": bidders, "awarded_to": None, "award_value": None,
                 "award_ref": None}

    # The schedule form states its own verdict; prefer that to any inference.
    low = _LOWEST.search(text or "")
    if low:
        out["awarded_to"] = low.group(1).strip()
        out["award_value"] = parse_money(low.group(2))
    else:
        # Multi-sheet tenders (one BoQ per package) legitimately carry several
        # L1 rows for different lots. There is no single winner to state then,
        # so the comparative statement is kept in full and the summary columns
        # stay empty rather than arbitrarily promoting the first row.
        firsts = [b for b in bidders if b.get("rank") == "L1"]
        if len(firsts) == 1:
            out["awarded_to"] = firsts[0]["bidder"]
            out["award_value"] = firsts[0]["amount"]

    ref = _CONTRACT_NO.search(text or "")
    if ref:
        out["award_ref"] = ref.group(1).strip()
    return out


def _pdf_text(path: Path) -> str:
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages[:4])
    except Exception as exc:  # noqa: BLE001 - an unreadable AOC is still archived
        log.debug("AOC text extraction failed for %s: %s", path, exc)
        return ""


def _document_row(conn, tender_id: str, filename: str, url: str) -> int | None:
    """Upsert the AOC document row and return its id."""
    conn.execute(
        "INSERT INTO documents (tender_id, filename, section, description,"
        " download_url, status, source, first_seen_at, link_first_seen_at)"
        " VALUES (?, ?, ?, ?, ?, 'pending', 'scraped', ?, ?)"
        " ON CONFLICT(tender_id, filename, section) DO UPDATE SET"
        "   download_url = excluded.download_url",
        (tender_id, filename, AOC_SECTION,
         "Published by the department under Results of Tenders",
         url, now_iso(), now_iso()),
    )
    row = conn.execute(
        "SELECT id FROM documents WHERE tender_id=? AND filename=? AND section=?",
        (tender_id, filename, AOC_SECTION)).fetchone()
    return row["id"] if row else None


def probe_tender(conn, client: HttpClient, cfg, tender_id: str,
                 sp_token: str) -> dict:
    """Fetch, store and parse one tender's AOC document. One request.

    Always stamps ``award_probed_at``, hit or miss — that is what makes a sweep
    resumable and stops a tender with no award being asked for one every run.
    """
    url = aoc_url(cfg.host, sp_token)
    now = now_iso()
    resp = client.get(url)
    conn.execute(
        "INSERT INTO fetch_log (url, tender_id, http_status, kind, fetched_at)"
        " VALUES (?, ?, ?, 'award', ?)", (url, tender_id, resp.status_code, now))

    body = resp.content
    is_pdf = ("pdf" in resp.headers.get("Content-Type", "").lower()
              and body[:5] == b"%PDF-")
    if resp.status_code != 200 or not is_pdf:
        conn.execute("UPDATE tenders SET award_probed_at=? WHERE tender_id=?",
                     (now, tender_id))
        conn.commit()
        return {"tender_id": tender_id, "award": False}

    published_name = _disposition_name(resp) or "AOC.pdf"
    fname = safe_filename(published_name)
    doc_id = _document_row(conn, tender_id, fname, url)
    if doc_id is None:  # pragma: no cover - the upsert above just ran
        conn.commit()
        return {"tender_id": tender_id, "award": False, "error": "no_doc_row"}

    # A dedicated subdirectory, not the tender's document root: the AOC file
    # arrives with whatever name the department gave it ("SUMMARY.pdf"), which
    # can collide with a tender document, and store_document writes by path.
    target = Path(cfg.docs_dir) / tender_id / "aoc" / fname
    outcome = store_document(conn, cfg, doc_id, target, body,
                             resp.headers.get("Content-Type"))

    signed_at, signer = signature(body)
    text = _pdf_text(target)
    # Guard against attributing an award to the wrong tender: when the generated
    # statement names a Tender ID, it must be this one.
    stated = _PDF_TENDER_ID.search(text)
    if stated and stated.group(1) != tender_id:
        log.warning("AOC document for %s names %s; not storing award fields",
                    tender_id, stated.group(1))
        conn.execute("UPDATE tenders SET award_probed_at=? WHERE tender_id=?",
                     (now, tender_id))
        conn.commit()
        return {"tender_id": tender_id, "award": False, "error": "tender_id_mismatch"}

    award = parse_award(text)
    bidders = award["bidders"]
    value = award["award_value"]
    # Only the department's own contract number, never the published filename.
    # Falling back to it put "AOC.pdf"/"PO27.pdf"/"stage1.pdf" into 31 of the
    # first 33 rows, and the UI renders this field as a contract reference —
    # captioning a filename as a contract number on an archive people cite is
    # worse than showing nothing. The filename is not lost: it is the stored
    # document's own name.
    ref = award["award_ref"]
    conn.execute(
        """
        UPDATE tenders SET
            award_stage = 'AOC',
            awarded_at = COALESCE(:signed, awarded_at),
            awarded_to = COALESCE(:who, awarded_to),
            award_value_num = COALESCE(:num, award_value_num),
            award_value_raw = COALESCE(:raw, award_value_raw),
            award_ref = :ref,
            award_signatory = COALESCE(:signer, award_signatory),
            award_bidders_json = :bidders,
            award_probed_at = :now,
            last_updated_at = :now
        WHERE tender_id = :tid
        """,
        {"signed": signed_at, "who": award["awarded_to"], "num": value,
         "raw": f"{value:,.2f}" if value is not None else None,
         "ref": ref, "signer": signer,
         "bidders": json.dumps(bidders, ensure_ascii=False) if bidders else None,
         "now": now, "tid": tender_id},
    )
    conn.commit()
    return {"tender_id": tender_id, "award": True, "bytes": len(body),
            "outcome": outcome, "bidders": len(bidders),
            "awarded_to": award["awarded_to"], "award_value": value,
            "awarded_at": signed_at, "ref": ref}


# A tender that has not closed cannot have been awarded, so the queue is closed
# tenders only. Dateless ones stay in: they come from the status-list backfills,
# which is precisely the set the AOC surface exists to describe, and excluding
# them would drop the highest-yield candidates the archive has.
_QUEUE_SQL = """
    SELECT tender_id, sp_token FROM tenders
    WHERE award_probed_at IS NULL
      AND sp_token IS NOT NULL
      AND cancelled_at IS NULL
      AND (closing_date IS NULL
           OR datetime(closing_date) < datetime('now', '+5 hours', '+30 minutes'))
    ORDER BY (closing_date IS NOT NULL), closing_date DESC, tender_id
"""


def candidates(conn, limit: int = 0) -> list[dict]:
    sql = _QUEUE_SQL + (" LIMIT ?" if limit else "")
    rows = conn.execute(sql, (limit,) if limit else ()).fetchall()
    return [dict(r) for r in rows]


def reparse_stored(db_path=None, *, progress=None) -> dict:
    """Re-read every AOC document already on disk. Zero requests.

    The generated forms vary more than they look — a narrow column glues two
    cells together in the text layer, a second layout ("SCHEDULE OF WORK") has
    to be recognised separately — so the parser will keep improving after the
    documents are captured. Re-reading bytes we already hold must never mean
    asking the portal for them again.
    """
    _p = progress or (lambda m: log.info("%s", m))
    cfg = load_config()
    db_path = db_path or cfg.db_path
    init_db(db_path)
    conn = connect(db_path)
    root = cfg.db_path.parent.parent
    gained = 0
    rows = conn.execute(
        "SELECT tender_id, stored_path FROM documents"
        " WHERE section=? AND status='captured' AND stored_path IS NOT NULL",
        (AOC_SECTION,)).fetchall()
    try:
        for r in rows:
            path = root / r["stored_path"]
            if not path.exists():
                continue
            award = parse_award(_pdf_text(path))
            if not award["bidders"] and not award["awarded_to"]:
                continue
            value = award["award_value"]
            before = conn.execute(
                "SELECT awarded_to FROM tenders WHERE tender_id=?",
                (r["tender_id"],)).fetchone()
            conn.execute(
                "UPDATE tenders SET awarded_to = COALESCE(:who, awarded_to),"
                " award_value_num = COALESCE(:num, award_value_num),"
                " award_value_raw = COALESCE(:raw, award_value_raw),"
                " award_ref = COALESCE(:ref, award_ref),"
                " award_bidders_json = :bidders WHERE tender_id = :tid",
                {"who": award["awarded_to"], "num": value,
                 "raw": f"{value:,.2f}" if value is not None else None,
                 "ref": award["award_ref"],
                 "bidders": json.dumps(award["bidders"], ensure_ascii=False)
                            if award["bidders"] else None,
                 "tid": r["tender_id"]})
            if award["awarded_to"] and not (before and before["awarded_to"]):
                gained += 1
        conn.commit()
    finally:
        conn.close()
    out = {"documents": len(rows), "newly_structured": gained}
    _p(f"awards reparse: {out}")
    return out


def run_enrich(db_path=None, *, limit: int = 50, tender_id: str | None = None,
               progress=None) -> dict:
    """Probe ``limit`` un-probed tenders for an award. Resumable and idempotent."""
    _p = progress or (lambda m: log.info("%s", m))
    cfg = load_config()
    db_path = db_path or cfg.db_path
    cfg.ensure_dirs()
    init_db(db_path)
    conn = connect(db_path)
    client = HttpClient(cfg)
    found = missed = errors = 0
    structured = 0
    try:
        if tender_id:
            row = conn.execute(
                "SELECT tender_id, sp_token FROM tenders WHERE tender_id=?",
                (tender_id,)).fetchone()
            queue = [dict(row)] if row and row["sp_token"] else []
        else:
            queue = candidates(conn, limit)
        _p(f"awards: probing {len(queue)} tender(s)")
        for row in queue:
            try:
                res = probe_tender(conn, client, cfg, row["tender_id"],
                                   row["sp_token"])
            except RequestCapExceeded:
                _p("request cap reached; stopping award enrichment")
                break
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.warning("award probe failed for %s: %s", row["tender_id"], exc)
                continue
            if res.get("award"):
                found += 1
                if res.get("awarded_to"):
                    structured += 1
                    _p(f"  {res['tender_id']}  ✓ {res['awarded_to']}"
                       f"  ₹{res['award_value']:,.2f}  {res['ref']}")
                else:
                    _p(f"  {res['tender_id']}  ✓ {res['ref']} "
                       f"({res.get('bidders', 0)} bidders parsed)")
            else:
                missed += 1
    finally:
        conn.close()
    out = {"probed": found + missed, "awards": found, "structured": structured,
           "no_award": missed, "errors": errors, "requests": client.request_count}
    _p(f"awards: {out}")
    return out
