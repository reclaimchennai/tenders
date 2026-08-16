"""FTS5 query building and search execution for the web app.

User input is sanitized into safe FTS5 MATCH syntax: quoted phrases are kept as
phrases, bare words become prefix terms ("term"*), and all FTS operator
characters are stripped so a stray quote or AND can never raise a syntax error.
"""

from __future__ import annotations

import re
from typing import Sequence

from .excerpt import Term, compile_terms, excerpt, matches, opening

_PHRASE = re.compile(r'"([^"]+)"')
# Characters with special meaning to FTS5 that we strip from bare terms.
_FTS_SPECIAL = re.compile(r'[\^\*\(\)\":\-+]')
# What the index's unicode61 tokenizer will make of a term once FTS5 has it.
_TOKENS = re.compile(r"[^\W_]+")


def _parse(query: str) -> list[tuple[str, Term]]:
    """Each part of the query as ``(FTS5 fragment, what it will match)``.

    Both halves come out of one pass on purpose. The excerpt builder has to
    search a document for exactly what FTS5 searched the index for, and the only
    way to be sure of that is for the two descriptions of a term to be produced
    together — a second parser over the user's string would be free to drift.
    """
    if not query or not query.strip():
        return []
    parts: list[tuple[str, Term]] = []

    # Pull out quoted phrases first.
    for m in _PHRASE.finditer(query):
        phrase = m.group(1).strip()
        phrase = phrase.replace('"', "")
        if phrase:
            parts.append((f'"{phrase}"', Term(tuple(_TOKENS.findall(phrase)), False)))
    rest = _PHRASE.sub(" ", query)

    # Every bare word is emitted quoted. Stripping punctuation is not enough to
    # make a word safe: AND, OR, NOT and NEAR are FTS5 operators as *barewords*,
    # so "road AND bridge" — an entirely reasonable thing to type into a search
    # box — compiled to `road* AND* bridge*` and raised a syntax error, which
    # the site served as a 500. Quoting turns each word back into a literal, and
    # "word"* is the phrase-prefix form, so matching is unchanged for every
    # query that already worked.
    for token in rest.split():
        clean = _FTS_SPECIAL.sub("", token).strip()
        tokens = tuple(_TOKENS.findall(clean))
        if len(clean) >= 2:
            parts.append((f'"{clean}"*', Term(tokens, True)))
        elif clean:
            parts.append((f'"{clean}"', Term(tokens, False)))

    return parts


def build_match(query: str) -> str | None:
    """Turn free text into a safe FTS5 MATCH expression (implicit AND)."""
    parts = [fragment for fragment, _ in _parse(query)]
    return " ".join(parts) if parts else None


def parse_terms(query: str) -> list[Term]:
    """What ``build_match(query)`` will actually match, for the excerpt builder."""
    return [term for _, term in _parse(query)]


# Ranking an FTS5 match in the same SELECT that joins the base table defeats
# FTS5's own ordering: SQLite cannot push `ORDER BY rank` into the virtual table
# across a join, so it falls back to a temp b-tree — and because snippet() is in
# the select list, it builds a snippet for *every* match before throwing all but
# 25 away. Ranking and paginating against the FTS table alone lets FTS5 consume
# the ORDER BY, so snippet() runs on one page's worth of rows. Measured on a
# 15,341-document corpus, a query matching 5,818 documents: 203 ms -> 6 ms, and
# flat instead of linear in the offset (page 100 was 624 ms, now still ~6 ms).
_TENDER_HITS = """
    SELECT tender_id,
           snippet(tenders_fts, 2, '<mark>', '</mark>', ' … ', 12) AS snip,
           rank AS rank
    FROM tenders_fts WHERE tenders_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?
"""

# work_description/product_category/tender_category are here for the headline
# composer, not for display: a tender titled "OT 44/25-26" can only be named
# from them (see shortnames.headline). They cost nothing — same row, no join.
_TENDER_COLUMNS = """
    t.tender_id, t.title, t.short_name, t.organisation_chain, t.location,
    t.tender_value_raw, t.tender_type, t.published_date, t.closing_date,
    t.status, t.source, t.cancelled_at, t.awarded_at, t.awarded_to,
    t.award_value_num, t.work_description, t.product_category, t.tender_category
"""


def search_tenders(conn, match: str, limit: int, offset: int) -> tuple[list[dict], int]:
    total = conn.execute(
        "SELECT count(*) FROM tenders_fts WHERE tenders_fts MATCH ?", (match,)
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        WITH hits AS MATERIALIZED ({_TENDER_HITS})
        SELECT {_TENDER_COLUMNS}, h.snip AS snip, h.rank AS rank
        FROM hits h JOIN tenders t ON t.tender_id = h.tender_id
        ORDER BY h.rank
        """,
        (match, limit, offset),
    ).fetchall()
    return [dict(r) for r in rows], total


def distinct_values(conn, column: str) -> list[str]:
    """Distinct non-empty values of a whitelisted column, for filter dropdowns."""
    allowed = {"tender_type", "tender_category", "product_category"}
    if column not in allowed:
        return []
    rows = conn.execute(
        f"SELECT DISTINCT {column} FROM tenders WHERE {column} IS NOT NULL "
        f"AND {column} <> '' ORDER BY {column}").fetchall()
    return [r[0] for r in rows]


# GePNIC detail-page fields kept verbatim in raw_json — every one is searchable
# via json_extract. Whitelisted here to keep paths injection-safe.
RAW_KEYS = {
    "form_of_contract": "Form Of Contract",
    "payment_mode": "Payment Mode",
    "contract_type": "Contract Type",
    "sub_category": "Sub category",
}
# GePNIC "Selection Criteria" yes/no flags.
CRITERIA = {
    "two_stage": "Allow Two Stage Bidding",
    "nda": "Should Allow NDA Tender",
    "preferential": "Allow Preferential Bidder",
    "gte": "General Technical Evaluation Allowed",
    "ite": "ItemWise Technical Evaluation Allowed",
    "fee_exempt": "Tender Fee Exemption Allowed",
    "emd_exempt": "EMD Exemption Allowed",
    "withdrawal": "Withdrawal Allowed",
}


def raw_json_options(conn, key: str) -> list[str]:
    """Distinct values of a whitelisted raw_json field, for a dropdown."""
    json_key = RAW_KEYS.get(key)
    if not json_key:
        return []
    path = '$."' + json_key.replace('"', '') + '"'
    rows = conn.execute(
        "SELECT DISTINCT json_extract(raw_json, ?) v FROM tenders "
        "WHERE v IS NOT NULL AND v <> '' AND v <> 'NA' ORDER BY v", (path,)).fetchall()
    return [r[0] for r in rows]


def organisation_options(conn) -> list[str]:
    """Distinct top-level departments (first segment of the ``||`` org chain)."""
    rows = conn.execute(
        """
        SELECT DISTINCT
          TRIM(substr(organisation_chain, 1,
               CASE WHEN instr(organisation_chain, '||') > 0
                    THEN instr(organisation_chain, '||') - 1
                    ELSE length(organisation_chain) END)) AS dept
        FROM tenders
        WHERE organisation_chain IS NOT NULL AND organisation_chain <> ''
        ORDER BY dept
        """
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def _many(value: str | Sequence[str] | None) -> list[str]:
    """Normalise a filter argument to a list of wanted values.

    Every one of these fields accepts either a single string — which is what an
    old ``?org=X`` link and every pre-existing caller passes — or a sequence of
    them meaning "any of these". Blanks are dropped rather than matched: the
    dropdowns submit an empty value for "Any", and a filter for the empty string
    would return nothing at all.
    """
    if not value:
        return []
    if isinstance(value, str):
        value = (value,)
    return [v for v in (str(x).strip() for x in value) if v]


def _prefix_range(prefix: str) -> tuple[str, str]:
    """Half-open ``[prefix, next)`` bounds equivalent to ``LIKE 'prefix%'``.

    A range is what ``idx_tenders_org`` can actually seek. ``LIKE`` cannot use it
    at all — SQLite only considers an index for LIKE when the pattern is
    case-sensitive, and the default is not — so every organisation filter used to
    scan the whole index: 8.6 ms for one department, 15.8 ms for three, growing
    with the archive. As bounds it is 0.8 ms and 2.6 ms, on identical result
    counts (13,674 and 16,087 measured against 77,321 tenders).

    Two behaviour changes come with it, both wanted. The match is now
    case-sensitive, which is what the values are: every organisation link on this
    site is built from a stored chain or picked from a list of them. And ``_`` and
    ``%`` stop being wildcards — under LIKE, filtering by the real department
    "Dean_VCRI_Theni" quietly matched any character where the underscores were.

    UTF-8 orders codepoints, so incrementing the last one bounds the range for
    non-ASCII names too.
    """
    return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _any_of(column: str, values: list[str]) -> str:
    """``col IN (?,?,?)`` — placeholders counted, never a user value spliced in."""
    return f"{column} IN ({','.join('?' * len(values))})"


def search_tenders_advanced(conn, match: str | None,
                            org: str | Sequence[str] = "", date_from: str = "",
                            date_to: str = "", category: str | Sequence[str] = "",
                            captured: str = "",
                            tender_type: str | Sequence[str] = "",
                            product_category: str | Sequence[str] = "",
                            tender_id: str = "", ref_number: str = "", pincode: str = "",
                            value_min: str = "", value_max: str = "",
                            form_of_contract: str | Sequence[str] = "",
                            payment_mode: str | Sequence[str] = "",
                            criteria: tuple = (),
                            limit: int = 25, offset: int = 0
                            ) -> tuple[list[dict], int]:
    """Search tenders by any combination of the GePNIC advanced-search fields we
    hold data for (keyword FTS, organisation, date range, category, product
    category, tender type, tender id, reference number, pincode, value range,
    captured-docs). Any subset may be empty.

    The six list-valued fields OR *within* themselves and AND *across* — picking
    two departments and two categories asks for "either department, and either
    category", which is what a set of tag chips looks like it should mean.
    """
    where: list[str] = []
    params: list = []
    if match:
        joins = "JOIN tenders_fts ON tenders_fts.tender_id = t.tender_id"
        where.append("tenders_fts MATCH ?")
        params.append(match)
        select_snip = ("snippet(tenders_fts, 2, '<mark>', '</mark>', ' … ', 12) AS snip, "
                       "bm25(tenders_fts) AS rank")
    else:
        joins = ""
        select_snip = "NULL AS snip, 0 AS rank"
    # Organisation is a prefix match, not equality: the chips in a listing filter
    # by the chain up to and including themselves, so "this department and
    # everything under it".
    orgs = _many(org)
    if orgs:
        where.append("(" + " OR ".join(
            ["(t.organisation_chain >= ? AND t.organisation_chain < ?)"] * len(orgs)) + ")")
        for o in orgs:
            lo, hi = _prefix_range(o)
            params.extend((lo, hi))
    if date_from:
        where.append("substr(t.published_date, 1, 10) >= ?")
        params.append(date_from)
    if date_to:
        where.append("substr(t.published_date, 1, 10) <= ?")
        params.append(date_to)
    for column, wanted in (("t.tender_category", category),
                           ("t.tender_type", tender_type),
                           ("t.product_category", product_category)):
        values = _many(wanted)
        if values:
            where.append(_any_of(column, values))
            params.extend(values)
    if tender_id:
        where.append("t.tender_id LIKE ?")
        params.append(f"%{tender_id}%")
    if ref_number:
        where.append("t.reference_number LIKE ?")
        params.append(f"%{ref_number}%")
    if pincode:
        where.append("t.pincode = ?")
        params.append(pincode)
    if value_min:
        try:
            where.append("t.tender_value_num >= ?")
            params.append(float(value_min))
        except ValueError:
            where.pop()
    if value_max:
        try:
            where.append("t.tender_value_num <= ?")
            params.append(float(value_max))
        except ValueError:
            where.pop()
    # The json_extract expressions are spelled exactly as idx_tenders_form_of_contract
    # and idx_tenders_payment_mode declare them; SQLite only matches an expression
    # index on a byte-identical expression, so reformatting these silently drops
    # them to a full scan.
    for expr, wanted in (
            ("json_extract(t.raw_json, '$.\"Form Of Contract\"')", form_of_contract),
            ("json_extract(t.raw_json, '$.\"Payment Mode\"')", payment_mode)):
        values = _many(wanted)
        if values:
            where.append(_any_of(expr, values))
            params.extend(values)
    for crit in criteria:
        json_key = CRITERIA.get(crit)
        if json_key:
            where.append(f"json_extract(t.raw_json, '$.\"{json_key}\"') = 'Yes'")
    if captured == "yes":
        # Deliberately IN and not EXISTS: the list subquery is built once off
        # idx_docs_status, while the correlated EXISTS re-probes per tender
        # (measured at 10x corpus: 3.9 ms vs 45.4 ms).
        where.append("t.tender_id IN (SELECT tender_id FROM documents WHERE status='captured')")
    clause = " AND ".join(where) if where else "1=1"

    # Date order needs a tiebreaker to be an order at all: closing_date is far
    # from unique (4,079 tenders share NULL, 337 share a single timestamp), so
    # without tender_id the sequence within a tie is whatever the plan happens
    # to emit. That is what makes paging lossy — the scraper inserts a row, the
    # arbitrary order shifts under the offset, and page 3 skips a tender page 2
    # never showed. On (closing_date, tender_id) the order is total, so walking
    # the pages visits every tender exactly once.
    #
    # Which plan should produce that order depends on whether anything is
    # filtered, and getting it wrong is expensive in one specific direction.
    # Unfiltered, idx_tenders_closing_id yields the first 25 rows by walking 25
    # index entries. Filtered, the planner still reaches for that index and then
    # discards rows that fail the filter — so a filter that matches *nothing*
    # walks the entire index before admitting defeat (32 ms, and it grows with
    # the archive). The `+` disables the index for ordering and sorts the
    # filtered set instead, which is bounded by how much the filter kept:
    #   filter            index-ordered   sorted
    #   none                     0.0 ms   10.9 ms
    #   category=Goods           2.1 ms    1.2 ms
    #   criteria, 0 matches     32.5 ms   12.8 ms
    #   EMD-exempt, 2,297       8.9 ms    3.4 ms
    if match:
        order = "ORDER BY rank"
    elif where:
        order = "ORDER BY +t.closing_date DESC, +t.tender_id DESC"
    else:
        order = "ORDER BY t.closing_date DESC, t.tender_id DESC"

    # A bare keyword search — no other filter — is the common case, and it is the
    # one FTS5 can rank and paginate entirely by itself. See _TENDER_HITS: doing
    # it that way keeps snippet() off every match. It only works when nothing
    # outside the FTS index has to be consulted before the LIMIT, so anything
    # touching `tenders` falls through to the general query below. Applying the
    # same shape to the filtered path measured *slower* (39 ms -> 63 ms at 10x):
    # SQLite re-runs the match to attach snippets instead of seeking by rowid.
    if match and len(where) == 1:
        total = conn.execute(
            "SELECT count(*) FROM tenders_fts WHERE tenders_fts MATCH ?", (match,)
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            WITH hits AS MATERIALIZED ({_TENDER_HITS})
            SELECT {_TENDER_COLUMNS}, h.snip AS snip, h.rank AS rank
            FROM hits h JOIN tenders t ON t.tender_id = h.tender_id
            ORDER BY h.rank
            """,
            (match, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows], total

    total = conn.execute(
        f"SELECT count(*) FROM tenders t {joins} WHERE {clause}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT t.tender_id, t.title, t.short_name, t.organisation_chain, t.location,
               t.tender_value_raw, t.tender_type, t.closing_date, t.published_date,
               t.status, t.source, t.cancelled_at, t.awarded_at,
               t.awarded_to, t.award_value_num, t.work_description,
               t.product_category, t.tender_category, {select_snip}
        FROM tenders t {joins}
        WHERE {clause}
        {order}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()
    return [dict(r) for r in rows], total


def search_documents(conn, match: str, limit: int, offset: int,
                     terms: Sequence[Term] = ()) -> tuple[list[dict], int]:
    """One page of document hits, each with an excerpt showing why it matched.

    The excerpt is built in Python (see excerpt.py) rather than by
    ``snippet()``, which was 3,778 ms of the 3,859 ms this function used to
    take. Matching, ranking and paging are untouched — same rows, same order,
    same total; only the text of ``snip`` changes.
    """
    total = conn.execute(
        "SELECT count(*) FROM docs_fts WHERE docs_fts MATCH ?", (match,)
    ).fetchone()[0]
    # Same rank-then-join shape as search_tenders: FTS5 can only consume the
    # ORDER BY when it ranks and paginates against the index alone.
    rows = conn.execute(
        """
        WITH hits AS MATERIALIZED (
            SELECT document_id, rank AS rank
            FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?
        )
        SELECT d.id AS document_id, d.tender_id, d.filename, d.section, d.status,
               t.title, t.organisation_chain, h.rank AS rank
        FROM hits h
        JOIN documents d ON d.id = h.document_id
        JOIN tenders t ON t.tender_id = d.tender_id
        ORDER BY h.rank
        """,
        (match, limit, offset),
    ).fetchall()
    docs = [dict(r) for r in rows]
    _attach_excerpts(conn, docs, terms)
    return docs, total


def _attach_excerpts(conn, docs: list[dict], terms: Sequence[Term]) -> None:
    """Fill in ``snip`` and ``snip_kind`` for one page of document hits.

    ``snip_kind`` is what keeps this honest. A result whose terms are nowhere in
    its text still has to say why it is here — it matched on the filename, which
    is the other indexed column — and a result we cannot explain at all has to
    admit that the text below is only the start of the document. ``snippet()``
    returned the opening of the document in both cases with nothing to
    distinguish it from a real hit, so a search that found nothing deep inside a
    scan looked exactly like one that did.
    """
    if not docs:
        return
    scans = compile_terms(terms)

    def describe(doc: dict, text: str) -> None:
        snip, kind = excerpt(text, scans), "text"
        if snip is None:
            if matches(doc.get("filename") or "", scans):
                snip, kind = None, "filename"
            else:
                snip, kind = opening(text), "opening"
        doc["snip"], doc["snip_kind"] = snip, kind

    by_id = {d["document_id"]: d for d in docs}
    # Extraction runs continuously against this database, so a row can be
    # rewritten between the match and the fetch. Defaulting every hit to
    # "no text" first means a document that disappears mid-request is described
    # by its filename rather than left with no explanation at all.
    for doc in docs:
        describe(doc, "")
    ids = list(by_id)
    # Streamed rather than collected into a dict: one page of results for a
    # common word is 9 MB of OCR, and holding all 25 texts at once for the sake
    # of 25 short excerpts was the peak this request was measured at — it cost
    # 17 ms of p95 on the rare-term shape.
    rows = conn.execute(
        f"SELECT document_id, text FROM doc_text "
        f"WHERE document_id IN ({','.join('?' * len(ids))})", ids)
    for doc_id, text in rows:
        describe(by_id[doc_id], text or "")
