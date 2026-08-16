"""Excerpts for document search hits, built in Python instead of by FTS5.

``snippet()`` was the whole cost of document search: 25 ranked ids came back in
26 ms, the same 25 rows with a snippet took 3,778 ms. The reason is that
``snippet()`` re-tokenises the entire column it is given, and one page of
results for a common word carries 9.1 million characters of OCR — a single
result can be a 3.1 MB scan. The token window makes no difference (6 tokens and
64 tokens measure the same); it is the document, not the excerpt, that is being
read.

Nothing about that work is needed to show a reader why a document matched. The
25 texts on the page fetch in 4 ms, and a scan finds the term in them in
single-digit milliseconds — so the excerpt is built here.

The catch is that "find the term" has to mean exactly what FTS5 meant, in both
directions. Miss a match and a phrase buried on page 40 of a scan comes back
with no excerpt, which reads as a false positive; invent one and the excerpt
claims a reason that was never the reason. Three rules reproduce it:

  * a token is a run of Unicode alphanumerics — that is what the
    ``unicode61`` tokenizer the index is built with considers a token, with
    underscore a separator rather than a token character;
  * ``build_match`` emits every bare word as ``"word"*``, a phrase-prefix, so
    ``drain`` legitimately matched *drainage* but never *underdrain*: the
    prefix binds at the start of a token only;
  * the index is built ``remove_diacritics 2``, so ``ä`` is indexed as ``a``,
    and it case-folds by its own table rather than Unicode's. Neither is a
    curiosity in this corpus: a large share of the Tamil notices are legacy
    font-encoded and come out of extraction as Latin-1 accented letters, and
    before the fold was handled every such document lost its excerpt.

So each term becomes a scan anchored to a token start, spelled in the
tokenizer's own character classes (see Scan). The leading token boundary is
checked in Python rather than written as a lookbehind, because a pattern that
starts with something matchable lets ``re`` skip ahead in C — on the largest
document in the corpus, finding a term 2.7 M characters in takes 24 ms that way
and 82 ms with ``(?<![^\\W_])`` in front of it.

Known gap, deliberately left to the fallback rather than approximated: a
character the tokenizer indexes as *two* (the ﬁ ligature indexes as ``fi``)
cannot be a character class, so a document that spells the term that way is
reported as unexplained instead of being marked in the wrong place.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from html import escape
from typing import Iterable, NamedTuple, Sequence

# unicode61's token character, and its complement. `\w` is wrong on its own:
# it counts underscore as a word character where the tokenizer counts it as a
# separator, so `storm_water` is two tokens to FTS5 and one to `\w`.
_TOKEN = r"[^\W_]"
_SEP = r"[\W_]"
_TOKEN_CHAR = re.compile(_TOKEN)
_WS = re.compile(r"\s+")

# Visible excerpt budget, in characters of whitespace-collapsed text.
_WINDOW = 320
# How much run-up to show before the first highlight. Enough for the sentence to
# start, not so much that the highlight falls off the end of a narrow card.
_LEAD = 90
# Raw characters lifted around the chosen match before whitespace is collapsed.
# OCR text is whitespace-heavy, so the slab has to be several times the window
# for the window to fill up.
_SLAB = 2400
# Occurrences collected per term. A 3 MB OCR blob can hold 35,000 copies of a
# common word; the window only ever shows one of them, so the scan stops as soon
# as it has enough to choose between. Uncapped, collecting them costs 96 ms
# against 0.3 ms capped.
_MAX_HITS = 24


# The tokenizer's diacritic table, asked of the tokenizer rather than restated
# here. Restating it was tried and was wrong in the one direction that matters:
# deriving folds from Unicode NFD over-folds 228 codepoints SQLite leaves alone
# (all of Greek Extended, some Latin Extended-B), and every one of those would
# have highlighted a passage FTS5 never matched. Built on first use, ~27 ms
# once per process, and only for a document search.
_FOLD: dict[str, str] = {}
_VARIANTS: dict[str, str] = {}
# Nothing above this folds — checked against the tokenizer across the whole BMP,
# where the only entries beyond it are obscure Latin Extended-D capitals that
# SQLite does not case-fold either. Stopping here costs 27 ms instead of 215 ms.
_FOLD_CEILING = 0x3000
# Handlers are sync `def`, so uvicorn runs them in a threadpool and two searches
# can arrive here at once. The tables are published only once they are complete,
# because a thread that saw a half-built _FOLD would compile a term against the
# wrong character classes and silently mis-highlight it.
_FOLD_LOCK = threading.Lock()


def _load_folds() -> None:
    if _FOLD:
        return
    with _FOLD_LOCK:
        if _FOLD:
            return
        db = sqlite3.connect(":memory:")
        db.execute("CREATE VIRTUAL TABLE f USING fts5(t, "
                   "tokenize='unicode61 remove_diacritics 2')")
        db.execute("CREATE VIRTUAL TABLE v USING fts5vocab(f, 'instance')")
        # From 1, not from 0x80: the class for `a` has to contain `a` and `A`,
        # and they are only in it if the tokenizer was asked about them too.
        db.executemany("INSERT INTO f(rowid, t) VALUES (?, ?)",
                       [(cp, chr(cp)) for cp in range(0x1, _FOLD_CEILING)
                        if chr(cp).isalnum()])
        fold: dict[str, str] = {}
        variants: dict[str, list[str]] = {}
        for term, doc in db.execute("SELECT term, doc FROM v"):
            char = chr(doc)
            # Multi-character tokens (the ﬁ ligature indexes as "fi") are left
            # out: a class holds one character, so they fall to the fallback
            # rather than being approximated.
            if len(term) == 1:
                variants.setdefault(term, []).append(char)
                if term != char:
                    fold[char] = term
        db.close()
        _VARIANTS.update((base, "".join(chars)) for base, chars in variants.items())
        _FOLD.update(fold)


class Term(NamedTuple):
    """One term of a parsed query: the tokens it matched, and whether the last
    of them was a prefix (``"drain"*``) rather than a whole token."""

    tokens: tuple[str, ...]
    prefix: bool


class Scan(NamedTuple):
    """One term's scan, as a fast locator and an exact test.

    ``exact`` spells every character as the class of characters the index folds
    it together with — taken from the tokenizer, so it matches a passage if and
    only if FTS5 matched it. ``probe`` is the plain literal under
    ``re.IGNORECASE``: much faster (24 ms against 35 ms on the largest document
    in the corpus, because ``re`` can skip ahead on a literal but not on a class
    of 60) and *nearly* the same set. Nearly is not good enough on its own —
    Python case-folds 350 codepoint pairs SQLite does not, dotless ``ı`` against
    ``i`` among them — so ``probe`` only proposes positions and ``exact`` rules
    on each one.
    """

    probe: re.Pattern[str]
    exact: re.Pattern[str]


def _chars(word: str) -> str:
    """``word`` as a pattern matching it however it was cased or accented.

    Each character becomes the class of every character the index folds to the
    same token, which is why the class replaces ``re.IGNORECASE`` rather than
    joining it: ``A`` is in the class for ``a`` because the tokenizer says so,
    and ``ı`` is absent from it for the same reason.
    """
    _load_folds()
    out = []
    for ch in word:
        base = _FOLD.get(ch, ch)
        cls = _VARIANTS.get(base) or "".join({ch, ch.lower(), ch.upper()})
        out.append(f"[{re.escape(cls)}]")
    return "".join(out)


def compile_term(term: Term) -> Scan | None:
    """The scan for one term, or None if it has no tokens to look for.

    Tokens are joined by ``[\\W_]+`` because FTS5 only records that they were
    adjacent, not what separated them — a phrase indexed from ``storm water``
    matches text that wrote it as ``storm-water`` or across a line break.
    """
    if not term.tokens:
        return None
    # A trailing lookahead is free (it is only tried where the body already
    # hit); a leading one is not, hence the check inside _find.
    tail = "" if term.prefix else f"(?!{_TOKEN})"
    sep = _SEP + "+"
    probe = re.compile(sep.join(re.escape(t) for t in term.tokens) + tail,
                       re.IGNORECASE)
    exact = re.compile(sep.join(_chars(t) for t in term.tokens) + tail)
    return Scan(probe, exact)


def compile_terms(terms: Iterable[Term]) -> list[Scan]:
    return [s for s in (compile_term(t) for t in terms) if s is not None]


def _find(text: str, scan: Scan, cap: int, ti: int) -> list[tuple[int, int, int]]:
    """Occurrences of one term. Every span returned is one FTS5 matched."""
    for locate in (scan.probe, scan.exact):
        hits: list[tuple[int, int, int]] = []
        for m in locate.finditer(text):
            start = m.start()
            # Mid-token: `drain` inside `underdrain`. FTS5 did not match here,
            # so highlighting it would be a lie about why the document is in
            # the results.
            if start and _TOKEN_CHAR.match(text, start - 1):
                continue
            # The probe only proposes; `exact` decides, and its span is the one
            # marked, since a class can match a different length than a literal.
            confirmed = scan.exact.match(text, start)
            if confirmed is None:
                continue
            hits.append((start, confirmed.end(), ti))
            if len(hits) >= cap:
                return hits
        if hits:
            return hits
    # The probe found nothing it could confirm; `exact` has now been run over
    # the whole text too, so the term genuinely is not here.
    return []


def _scan(text: str, scans: Sequence[Scan],
          cap: int = _MAX_HITS) -> list[tuple[int, int, int]]:
    """Up to ``cap`` occurrences of each term, as ``(start, end, term)``."""
    hits: list[tuple[int, int, int]] = []
    for ti, scan in enumerate(scans):
        hits.extend(_find(text, scan, cap, ti))
    hits.sort()
    return hits


def matches(text: str, scans: Sequence[Scan]) -> bool:
    """Whether any term occurs in ``text`` — used on the filename, the other
    indexed column, to tell a filename hit from an unexplained one."""
    return any(_find(text, scan, 1, 0) for scan in scans)


def _densest(hits: list[tuple[int, int, int]], n_terms: int) -> tuple[int, int]:
    """Span of the window covering the most distinct terms, earliest wins.

    Showing the first occurrence of the first term would be a poor excerpt for a
    multi-word query: the reader wants the place where the words are together,
    which is usually the passage they were looking for.
    """
    reach = _WINDOW - _LEAD
    best = (-1, hits[0][0], hits[0][1])
    seen: dict[int, int] = {}
    j = 0
    for i, (start, _end, _ti) in enumerate(hits):
        while j < len(hits) and hits[j][0] - start <= reach:
            seen[hits[j][2]] = seen.get(hits[j][2], 0) + 1
            j += 1
        if len(seen) > best[0]:
            best = (len(seen), start, hits[j - 1][1])
        if len(seen) == n_terms:
            break
        ti = hits[i][2]
        seen[ti] -= 1
        if not seen[ti]:
            del seen[ti]
    return best[1], best[2]


def _token_edge(text: str, pos: int, step: int, limit: int = 80) -> int:
    """Walk ``pos`` off the middle of a token, so a slab never starts or ends on
    half a word — a half word both reads as OCR damage and can look like a hit."""
    moved = 0
    while 0 < pos < len(text) and _TOKEN_CHAR.match(text, pos - 1) and moved < limit:
        pos += step
        moved += 1
    return max(0, min(len(text), pos))


def _mark(seg: str, hits: Iterable[tuple[int, int, int]], lo: int, hi: int) -> str:
    """Escaped ``seg[lo:hi]`` with ``<mark>`` around each hit inside it.

    Everything here is OCR of an arbitrary uploaded PDF and the template renders
    it ``|safe``, so the text between the marks is escaped rather than trusted.
    ``snippet()`` used to do this; nothing else on this path will.
    """
    out: list[str] = []
    cur = lo
    for start, end, _ti in hits:
        start, end = max(start, lo), min(end, hi)
        if end <= cur:
            continue  # before the window, or overlapping a mark already emitted
        if start >= hi:
            break
        out.append(escape(seg[cur:max(start, cur)]))
        out.append("<mark>")
        out.append(escape(seg[max(start, cur):end]))
        out.append("</mark>")
        cur = end
    out.append(escape(seg[cur:hi]))
    return "".join(out)


def excerpt(text: str, scans: Sequence[Scan]) -> str | None:
    """A highlighted, HTML-escaped excerpt around the best match, or None.

    None means the terms are genuinely not in this text — the document matched
    on its filename, or on a fold the tokenizer does and a literal scan does
    not. Callers must say so rather than quietly showing the opening paragraph,
    which is what ``snippet()`` did and what makes a deep match look false.
    """
    if not text or not scans:
        return None
    hits = _scan(text, scans)
    if not hits:
        return None

    anchor_start, anchor_end = _densest(hits, len(scans))
    lo = _token_edge(text, max(0, anchor_start - _SLAB // 4), 1)
    hi = _token_edge(text, min(len(text), lo + _SLAB), -1)
    seg = _WS.sub(" ", text[lo:hi])

    # Re-scanned in slab coordinates: a couple of thousand characters, so the
    # cost is nil, and it picks up neighbours that the capped whole-document
    # scan may have skipped past.
    local = _scan(seg, scans, cap=_WINDOW)
    if not local:
        # The anchor cannot vanish (collapsing whitespace leaves every token and
        # every separator run intact), but a slab that ends up all separator
        # would leave nothing to show.
        return None
    start, end = _densest(local, len(scans))

    wlo = max(0, start - _LEAD)
    whi = min(len(seg), max(wlo + _WINDOW, end + 30))
    if wlo:
        space = seg.find(" ", wlo, start)
        wlo = space + 1 if space >= 0 else wlo
    if whi < len(seg):
        space = seg.rfind(" ", end, whi)
        whi = space if space > end else whi

    body = _mark(seg, local, wlo, whi)
    prefix = "… " if lo or wlo else ""
    suffix = " …" if hi < len(text) or whi < len(seg) else ""
    return prefix + body + suffix


def opening(text: str, limit: int = _WINDOW) -> str | None:
    """The start of a document, escaped — the honest fallback when no term can
    be located in it, and only ever shown labelled as such."""
    if not text:
        return None
    seg = _WS.sub(" ", text[:limit * 4]).strip()
    if not seg:
        return None
    cut = seg.rfind(" ", 0, limit)
    if len(seg) > limit:
        seg = seg[:cut if cut > 0 else limit] + " …"
    return escape(seg)
