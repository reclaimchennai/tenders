"""Concise, human-readable headlines for tender listings.

Three tiers, cheapest first:

* ``heuristic_short`` — instant, offline: strips procurement boilerplate
  ("Providing", "Supply of", spec/reference numbers, ward-and-zone tails) and
  trims to a tidy label.
* ``headline`` — the same, plus a fallback for the ~5% of tenders whose *title*
  carries no information at all. See its docstring: those are not summarisation
  failures, they are empty inputs, and the fix is to compose the label from the
  fields that do carry meaning.
* ``run_shortnames`` — stores ``headline``'s answer in ``tenders.short_name``
  so the render path never recomputes it. Prioritises live/recent tenders;
  idempotent (only fills NULLs). Never on the render path.

Why there is no summarisation library here
------------------------------------------
The obvious reach is for extractive summarisation (``sumy``'s TextRank/LexRank/
Luhn). It cannot work on this corpus, and the reason is structural rather than a
matter of tuning: those algorithms rank the *sentences* of a document and return
the best ones, and a tender title is not a document. Measured on 200 real titles
of 120 characters or more — the only case where shortening is even wanted — 138
parse as a single sentence, so all three summarisers return the input verbatim,
and 40 contain no sentence boundary at all, so all three return an empty string.
A dependency that shortens nothing 69% of the time and blanks the headline 20%
of the time has not earned its place. What actually shortens a title is knowing
which *phrases* are procurement boilerplate, which is what the rules below
encode.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

from .config import load_config
from .db import connect, init_db

log = logging.getLogger("shortnames")

_WS = re.compile(r"\s+")
_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)")
# Reference numbers and the dates attached to them, in one alternation: this
# runs on every rendered row, and two passes over a 200-character title cost
# twice what one does.
_REFNO = re.compile(r"\b(?:no\.?|ref\.?|rc\.?no\.?|spec\.?\s*no\.?|e[.\s-]*tender)\s*"
                    r"[:.]?\s*[\w/.-]*\d[\w/.-]*"
                    r"|\bdt\.?\s*\d[\d./-]*", re.IGNORECASE)
# A leading run of pure filing reference — "Z.O.IV.C.No.B3/5896/2025 ",
# "ETS No 11/26-27 ", "A3/1375/2025 ". Departments prefix these to perfectly
# good titles, and they eat the whole width of a ticker card.
_LEAD_CODE = re.compile(r"^\s*(?:[A-Z][\w.]*[./][\w./-]*\d[\w./-]*|\d[\w./-]*[/-][\w./-]+)"
                        r"[\s,:-]+")
# Trailing municipal locator: "in Div-191, Unit-43, Zone-14.", "at Ward No 13,
# North Zone". Where the tender is is already its own field on every card.
_ADMIN_TAIL = re.compile(
    r"[\s,;-]+(?:in|at|of)?\s*(?:div|dvn|division|unit|zone|ward)\b[\s\w.,/&-]*$",
    re.IGNORECASE)
# Trailing count — "5 Nos", "7nos", "- 2 no.". Cut mid-phrase by any trim it
# becomes a dangling numeral, which reads as a truncation bug.
_QTY_TAIL = re.compile(r"[\s,;-]*\b\d+\s*nos?\.?\s*$", re.IGNORECASE)
_LEAD = re.compile(
    r"^\s*(?:providing|provision\s+of|supplying|supply\s+(?:of|and)|procurement\s+of|"
    r"construction\s+of|constructions?\s+of|purchase\s+of|execution\s+of|"
    r"hiring\s+of|engaging|engagement\s+of|selection\s+of|appointment\s+of|"
    r"work\s+contract\s+for|annual\s+maintenance\s+(?:contract\s+)?(?:of|for)?|"
    r"design[,\s]+supply[,\s]+(?:erection|installation)[\w,\s]*?of|"
    r"repairing\s+and\s+fixing|repair\s+and\s+maintenance\s+of|"
    r"improvements?\s+to|formation\s+of|laying\s+of|tender\s+for)\s+",
    re.IGNORECASE)
# Words a trimmed headline must not end on — "Improvements to…" states nothing.
_DANGLING = {"for", "at", "in", "of", "and", "or", "the", "to", "with", "on",
             "under", "from", "by", "a", "an", "including", "near", "&", "-",
             "–", "—", "/"}
_WORD = re.compile(r"[A-Za-z]{3,}")
_ALPHA = re.compile(r"[A-Za-z]")
_DIGIT = re.compile(r"\d")
# Departments fill work_description with a pointer to the attachment — "REFER
# PDF", "As per tender specification", "As per NIT". These pass every other
# test for a real description (two words, no digits) and would then be promoted
# to the headline of a tender whose title had nothing, which is worse than the
# reference number it replaced. Length-bounded so a genuine title that opens
# "As per the approved estimate for …" is untouched.
_PLACEHOLDER = re.compile(r"^\s*(?:as\s+per|please\s+refer|kindly\s+refer|refer|see|"
                          r"details?\s+(?:as\s+per|in)|attached)\b", re.IGNORECASE)
_TRIM_EDGES = " -–—.,:;_/&"


def _content_words(text: str) -> int:
    """Words that carry meaning — three letters or more, digits excluded."""
    return len(_WORD.findall(text or ""))


def is_uninformative(text: str | None) -> bool:
    """True when a string names nothing: a filing code, or one bare word.

    "OT 44/25-26" and "7nos" are the shape this catches. The digit test is what
    separates a reference from a name — "MS Bolt and Nuts 16X113 MM" is a real
    description that happens to contain numbers, "A3/1375/2025" is not.
    """
    if not text or not text.strip():
        return True
    if _content_words(text) < 2:
        return True
    if len(text) <= 45 and _PLACEHOLDER.match(text):
        return True
    # The digit-heavy test exists to separate a filing reference from a name,
    # and a filing reference is short. Above this length it can only ever answer
    # "informative", so skipping it is both faster and one less way to misjudge
    # a long title that happens to quote a lot of measurements.
    if len(text) > 80:
        return False
    letters = len(_ALPHA.findall(text))
    digits = len(_DIGIT.findall(text))
    return bool(digits and digits >= letters)


def _strip_brackets(text: str) -> str:
    """Drop bracketed asides, but never the only content in the string.

    "SDRF 2026-2027(Manalurpet TP)" keeps its bracket: on a long title the
    parenthesis is a reference number, on a short one it is the entire subject.
    """
    if len(text) < 60:
        inner = "".join(m.group(0) for m in _BRACKETS.finditer(text))
        if inner and not is_uninformative(_BRACKETS.sub(" ", text)):
            return _BRACKETS.sub(" ", text)
        return text
    return _BRACKETS.sub(" ", text)


def _trim(text: str, max_words: int, max_chars: int) -> str:
    """Cut to a word/character budget, ending on a word that means something.

    The previous implementation cut at the first preposition instead — which on
    a title whose leading verb had just been stripped left exactly one word.
    "Drilling of New Borewell at Kannimar Nagar in Ward No 20" became
    "Drilling"; "Hiring of vehicle for the official use of Assistant Executive
    Engineer …" became "Vehicle". That single rule accounted for 12,252 of the
    archive's 15,611 meaningless headlines — the titles were fine, the trim
    destroyed them — so budgeting forwards and refusing to stop on a preposition
    is the whole fix.
    """
    words = text.split()
    if len(words) <= max_words and len(text) <= max_chars:
        return text
    kept: list[str] = []
    used = 0
    for w in words[:max_words]:
        cost = len(w) + (1 if kept else 0)
        if kept and used + cost > max_chars:
            break
        kept.append(w)
        used += cost
    # A trailing preposition or bare numeral is the visible signature of a
    # truncation; drop back past it rather than print "… at" or "… 5".
    while kept and (kept[-1].strip(_TRIM_EDGES).lower() in _DANGLING
                    or kept[-1].strip(_TRIM_EDGES).isdigit()):
        kept.pop()
    if not kept:
        return text[:max_chars].rsplit(" ", 1)[0]
    return " ".join(kept).strip(_TRIM_EDGES) + "…"


def _squash(text: str) -> str:
    """Collapse runs of whitespace and tidy the edges, scanning only if needed."""
    if "  " in text or "\t" in text or "\n" in text:
        text = _WS.sub(" ", text)
    return text.strip(_TRIM_EDGES)


# Every listing row calls this, so it is memoised on its own arguments — it is a
# pure function of them. Page 1 of /browse and the same twenty organisations
# recur across requests far more than the corpus size suggests, and the cache
# turns those into a dict lookup instead of eight regex passes.
@lru_cache(maxsize=8192)
def heuristic_short(title: str | None, max_words: int = 8, max_chars: int = 64) -> str:
    """A tidy label for one title. Deterministic, offline, no I/O."""
    if not title:
        return ""
    t = _strip_brackets(title.replace("_", " ") if "_" in title else title)
    t = _squash(_REFNO.sub(" ", t))

    stripped = _LEAD_CODE.sub("", t).strip(_TRIM_EDGES)
    if _content_words(stripped) >= 2:
        t = stripped
    # Each of these drops information, so each is conditional on what survives:
    # a title that is *only* boilerplate keeps its boilerplate.
    stripped = _LEAD.sub("", t).strip(_TRIM_EDGES)
    if _content_words(stripped) >= 3:
        t = stripped
    # Guarded rather than run unconditionally: the pattern is anchored at the
    # end, but Python's regex engine has no reverse scan and tries every start
    # position in a 200-character title to discover there is no "5 Nos" on it.
    if t[-4:].strip(" .").lower().endswith(("no", "nos")):
        stripped = _QTY_TAIL.sub("", t).strip(_TRIM_EDGES)
        if _content_words(stripped) >= 3:
            t = stripped
    if len(t) > max_chars:
        stripped = _ADMIN_TAIL.sub("", t).strip(_TRIM_EDGES)
        if _content_words(stripped) >= 4:
            t = stripped

    t = _trim(_squash(t), max_words, max_chars)
    # Only the first character, and only if it is lowercase: stripping "Hiring
    # of" leaves a sentence starting "vehicle for the official use of …", while
    # title-casing the whole string would turn "LT SHACKLE INSULATOR" and every
    # other departmental acronym into "Lt Shackle Insulator".
    if t[:1].islower():
        t = t[0].upper() + t[1:]
    return t or title[:max_chars]


def headline(row, max_words: int = 8, max_chars: int = 64) -> str:
    """The label a listing shows for a tender. Deterministic; safe per request.

    Roughly 4.8% of the archive's titles (3,372 of 70,517) name nothing at all —
    "Vehicle", "7nos", "OT 44/25-26", "WORKS19". No summariser can help with
    those: the portal's ``title`` field genuinely holds a store-room reference,
    and there is no text to condense. What the record *does* hold is a
    ``work_description`` (present and usable for 90% of the ones that were ever
    fetched in detail) and, failing that, the portal's own classification —
    "Maintenance Works" at "SEESMTPSII" says more than "OT 44/25-26" ever will.

    So this composes rather than summarises, and it falls through in order of
    how much the source is trusted: the department's title, then the
    department's own longer description of the work, then the portal's category
    and place. Only when all three are empty does the reference code stand, and
    then it is the honest answer — nothing better was ever published.

    ``row`` is any mapping (a sqlite3.Row or a dict) with the tender's columns.
    """
    # sqlite3.Row supports subscripting but not .get, and raises IndexError for a
    # column the query did not select — which a caller adding a new listing
    # should not have to discover as a 500.
    def get(key):
        try:
            return row[key]
        except (KeyError, IndexError):
            return None
    title = get("title") or ""
    source = title
    if is_uninformative(title):
        desc = get("work_description") or ""
        # Only if it is genuinely richer: some departments copy the reference
        # into work_description verbatim ("A3/1375/2025" in both).
        if not is_uninformative(desc) and len(desc) > len(title):
            source = desc

    short = heuristic_short(source, max_words, max_chars)
    if not is_uninformative(short):
        return short

    kind = (get("product_category") or get("tender_category") or "").strip()
    where = (get("location") or "").strip()
    parts = [p for p in (kind, where) if p]
    if parts:
        return _trim(" · ".join(parts), max_words + 2, max_chars)
    return short or title[:max_chars] or (get("tender_id") or "")


# ---------------------------------------------------------------------------
# Machine keys in human-facing fields
# ---------------------------------------------------------------------------
# A handful of departments file `location` and organisation-chain levels as
# database keys rather than names — "GREATER_CHENNAI_CORPORATION_ZONE_14". The
# underscore is the entire signal, and it is a reliable one: no clerk types one
# where a space belongs, so a value carrying underscores was never written to be
# read as it stands. Everything else is left exactly as the department published
# it, which is what the rest of this archive promises.
_SNAKE = re.compile(r"_+")
_LETTER_RUN = re.compile(r"[A-Za-z]+")
_VOWEL = re.compile(r"[AEIOU]")
# Re-casing a SHOUTED key must not swallow the abbreviations this corpus is
# built from. "TNEB Limited" rendered "Tneb Limited" would be a worse error than
# the underscore it fixed: people cite these pages, and a mangled department name
# is a wrong citation, while an ugly one is merely ugly.
#
# Seeded from every all-caps string the archive uses as a whole organisation-chain
# level (MAWS 13,054 tenders, TANGEDCO 6,805, PWD 2,413, TANTRANSCO, TWAD, ELCOT,
# TAMPCOL, AAZP, TNGECL, TCMPF, TANUVAS, DCAPS), plus the abbreviations that
# recur inside longer names. Vowel-less tokens — PWD, CMWSS, RDTN, DTP, SWD, TN,
# RD — are caught structurally below and need no entry here; this set exists for
# the ones that would otherwise pass as words.
_ACRONYMS = frozenset("""
AAZP AEE CMDA CMWSSB DCAPS ELCOT MAWS MTPS NCTPS SEESMTPSII TAMPCOL TANGEDCO
TANTRANSCO TANUVAS TCMPF TNEB TNGECL TNHB TNPL TNRDC TNSTC TTPS TWAD
""".split())
# Three letters or fewer and shouting is an office or a board, not a word — SE,
# CE, AEE, GCC, TOS, VP. English has almost nothing that short outside these,
# and what it does have is joining words, which are named here so "ZONE_14_OF_X"
# does not come out with an "OF" shouting in the middle of it.
_SHORT_WORDS = frozenset("AND FOR THE OF AT IN ON TO BY".split())


def _recase_word(word: str) -> str:
    """Title-case one SHOUTED word, unless it is an abbreviation."""
    if word in _ACRONYMS or not _VOWEL.search(word):
        return word
    if len(word) <= 3 and word not in _SHORT_WORDS:
        return word
    return word[0] + word[1:].lower()


# A trailing administrative subdivision: "Zone-VIII", "ZONE_14", "Ward No.31".
# The number is required. Without it the pattern eats real place names — the
# archive holds "East Zone Singanallur", where the ward-sized word IS the place.
_ZONE_TAIL = re.compile(
    r"(?i)[\s_,-]*\b(?:zone|ward)\b[\s_.-]*(?:no\.?)?[\s_.-]*"
    r"(?:\d+|[IVXLC]+)\.?\s*$")


def main_place(value: str | None) -> str:
    """Drop a trailing zone/ward qualifier for display: the organisation only.

    Display-only, deliberately. The zone and ward are the join key for the
    per-corporation ward maps this data is headed for, so they must survive in
    storage untouched — this only decides what the ticker shows.

    Left alone when the qualifier is the whole value ("ward 5.", "Zone-09"):
    stripping those yields an empty string, and a blank is worse than a bare
    ward number.
    """
    pretty = pretty_name(value)
    if not pretty:
        return pretty
    stripped = _ZONE_TAIL.sub("", pretty).strip(" _,-")
    return stripped or pretty


def pretty_name(value: str | None) -> str:
    """Render a stored location / organisation level as a person would write it.

    Two rules, in this order, and nothing else:

    * Underscores become spaces. Always safe — see above.
    * A value that arrived SHOUTED_LIKE_THIS is title-cased, abbreviations kept.
      Any lowercase letter anywhere in the value proves somebody already cased it
      deliberately, so "Dean_VCRI_Theni" only loses its underscores and keeps its
      VCRI.

    Shouting without an underscore is re-cased too. An earlier pass left those
    alone for fear of "Tangedco"/"Vellore,Rd,Tn", but the abbreviation guards
    below already cover both: TANGEDCO is named in _ACRONYMS, and RD/TN/PWD are
    caught structurally by having no vowel. What was left was department names
    genuinely bellowing at the reader — "CHENNAI RIVERS TRANSFORMATION COMPANY
    LIMITED" — which is not how anyone writes a name.
    """
    if not value:
        return ""
    spaced = _WS.sub(" ", _SNAKE.sub(" ", value)).strip()
    if any(ch.islower() for ch in value):
        return spaced
    return _LETTER_RUN.sub(lambda m: _recase_word(m.group(0)), spaced)


def run_shortnames(db_path=None, *, limit: int = 60, batch: int = 20) -> dict:
    """Fill short_name for tenders that lack one, live/recent first.

    Names come from ``headline`` — the rules above, run locally.

    This used to batch titles through the ``claude`` CLI when one happened to be
    on PATH, falling back to ``headline`` when it was not. That has been removed.
    It ran inside the continuous scrape loop, 80 tenders a cycle, so on a machine
    that had the CLI installed the archive's headlines were quietly produced by
    a hosted model, and on a machine that did not they were produced by these
    rules — the same archive rendering differently depending on what was in
    somebody's PATH, with nothing in the data recording which. For a public
    mirror people are meant to be able to stand up themselves, and which exists
    to be a *checkable* record, that is the wrong trade: a self-hoster running
    the documented command must get the archive the command describes, not a
    degraded version of one. Every other tier here was already local, the
    fallback was already this function, and the render path never touched the
    CLI at all — so removing it costs one tier of polish on a display label and
    buys the guarantee that nothing outside this repository shapes what the
    archive says.
    """
    cfg = load_config()
    db_path = db_path or cfg.db_path
    init_db(db_path)
    conn = connect(db_path)
    filled = 0
    try:
        rows = conn.execute(
            """
            SELECT tender_id, title, work_description, product_category,
                   tender_category, location FROM tenders
            WHERE short_name IS NULL AND title IS NOT NULL AND title <> ''
            ORDER BY (closing_date < datetime('now')),
                     (closing_date IS NULL), closing_date ASC
            LIMIT ?
            """, (limit,)).fetchall()
        if not rows:
            return {"filled": 0, "remaining": 0}

        for start in range(0, len(rows), batch):
            chunk = rows[start:start + batch]
            for r in chunk:
                conn.execute("UPDATE tenders SET short_name=? WHERE tender_id=?",
                             (headline(r), r["tender_id"]))
                filled += 1
            # Committed per batch, not per row and not once at the end: this
            # runs inside the scrape loop and must not hold the write lock
            # while the whole limit is worked through.
            conn.commit()
        remaining = conn.execute(
            "SELECT count(*) FROM tenders WHERE short_name IS NULL "
            "AND title IS NOT NULL AND title <> ''").fetchone()[0]
    finally:
        conn.close()
    return {"filled": filled, "remaining": remaining}
