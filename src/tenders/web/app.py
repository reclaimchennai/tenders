"""FastAPI mirror site: search, browse, tender detail, and document download.

Read-only against the SQLite DB (WAL allows concurrent reads while the scraper
writes). Server-rendered with Jinja2 — no client framework, for longevity.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import load_config
from ..db import ThreadLocalReader, init_db
from ..redflags import limited_tender, short_window
from ..shortnames import headline, main_place, pretty_name
from ..stats import gather_stats
from .dashboard import (
    _fmt_inr as _format_inr_short,
    _inr_exact,
    award_panel,
    dashboard_data,
    live_tenders,
    suspicious_history,
    tender_state,
)
from .cache import DASHBOARD_TTL, OPTIONS_TTL, STATS_TTL, cached
from .dates import closing_line, critical_dates, fmt_date, fmt_datetime, relative_close
from .docview import INLINE_MEDIA, build_preview, preview_kind
from .sharecard import is_fresh, og_meta, og_path, og_token, render_og
from .search import (
    CRITERIA,
    build_match,
    distinct_values,
    organisation_options,
    parse_terms,
    raw_json_options,
    search_documents,
    search_tenders_advanced,
)

cfg = load_config()
PROJECT_ROOT = cfg.db_path.parent.parent
HERE = Path(__file__).parent


def _format_filesize(byte_size: int | None, declared_kb: str | None = None) -> str:
    """Human file size from the bytes we actually captured.

    The portal's own ``declared_size`` is an unreliable KB string (and absent for
    zip bundles), so it is only a fallback for documents we never captured."""
    kb = byte_size / 1024 if byte_size else None
    if kb is None and declared_kb:
        try:
            kb = float(declared_kb)
        except ValueError:
            # The portal writes literal "NA" here for documents it never sized;
            # echoing it back as "NA KB" states a size that does not exist.
            return "" if declared_kb.strip().upper() in ("NA", "NIL", "-") \
                else f"{declared_kb} KB"
    if kb is None:
        return ""
    return f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"


app = FastAPI(title="TN Tenders Mirror")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
# Cache-bust CSS by its mtime so style changes always reach the browser.
try:
    templates.env.globals["css_v"] = int((HERE / "static" / "style.css").stat().st_mtime)
except OSError:
    templates.env.globals["css_v"] = 1
templates.env.filters["inr"] = _format_inr_short
templates.env.filters["filesize"] = _format_filesize
templates.env.filters["inr_exact"] = _inr_exact
# Dates are formatted for display only — never parsed back, sorted or compared.
templates.env.filters["day"] = fmt_date
templates.env.filters["daytime"] = fmt_datetime
# Display only. The stored string stays the filter value and the link target —
# a chip that reads "Greater Chennai Corporation Zone 14" must still search for
# the key the database actually holds.
templates.env.filters["pretty"] = pretty_name
# Ticker variant: organisation without the zone/ward tail. Same display-only
# rule — the subdivision is the join key for the planned ward maps, so it is
# dropped from a flap, never from the record.
templates.env.filters["place"] = main_place
templates.env.globals["tender_state"] = tender_state
templates.env.globals["closing_line"] = closing_line
templates.env.globals["relative_close"] = relative_close
# A whole row, not a title: ~5% of tenders are named by a filing reference and
# the only way to label those is from their other columns (see headline).
templates.env.globals["headline"] = headline


@app.middleware("http")
async def _cache_policy(request, call_next):
    """HTML must revalidate so versioned CSS/JS updates always reach the browser."""
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-cache"
    elif request.url.path.startswith("/static/") and "v" in request.query_params:
        # Only the ?v=-stamped URLs are immutable, and they genuinely are: the
        # stamp is style.css's mtime, so a deploy changes every asset URL. The
        # unstamped ones (pdf.worker.min.js, which pdf.js requests by a fixed
        # path of its own) must keep revalidating or a stale worker outlives its
        # library. Upstream this route only had Cloudflare's 4-hour default.
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp

# Ensure schema/migrations (e.g. short_name column) before the read-only handle.
init_db(cfg.db_path)

# Subscriptions, watches and tender alerts. Imported after init_db because the
# router opens its own write connection against the tables it creates.
from .watch_api import _vapid as _push_identity, router as watch_router  # noqa: E402

app.include_router(watch_router)
# Drives whether the UI offers to notify at all: a mirror without a VAPID key
# must not show a button that can only ever fail.
templates.env.globals["push_enabled"] = _push_identity is not None
# FastAPI runs these sync endpoints in a threadpool, so every handler needs its
# own reader — see ThreadLocalReader for what sharing one costs in correctness.
_reader = ThreadLocalReader(cfg.db_path)
PAGE_SIZE = int(cfg.web["page_size"])


def _stats() -> dict:
    """Corpus counts for the header strip.

    Eleven aggregates over the two biggest tables, on a page whose own query is
    now ~1 ms; recomputing them per request made them the page.
    """
    return cached("stats", STATS_TTL, lambda: gather_stats(cfg.db_path))


def _pagination(total: int, page: int, size: int) -> dict:
    pages = max(1, (total + size - 1) // size)
    return {"page": page, "pages": pages, "total": total,
            "has_prev": page > 1, "has_next": page < pages}


def _dash() -> dict:
    return cached("dash", DASHBOARD_TTL, lambda: dashboard_data(_reader()))


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """Home = procurement dashboard (summary, charts, live ticker, watchlist)."""
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"stats": _stats(), "dash": _dash(),
         "ticker": cached("ticker", DASHBOARD_TTL, lambda: live_tenders(_reader(), 28))},
    )


@app.get("/api/dashboard")
def api_dashboard():
    return JSONResponse(_dash())


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    """Archive of closed tenders carrying any suspicion signal."""
    return templates.TemplateResponse(
        request, "history.html",
        {"rows": cached("history", DASHBOARD_TTL,
                        lambda: suspicious_history(_reader(), 150))},
    )


# The Advanced panel's dropdowns are whole-table DISTINCTs — the two raw_json
# ones scan and JSON-parse every tender. They were 137 ms of an unfiltered
# /browse (541 ms each at 10x corpus) to render option lists that change only
# when the scraper finds a department or contract form it has never seen.
def _options() -> dict:
    def build():
        conn = _reader()
        return {
            "orgs": organisation_options(conn),
            "categories": [r[0] for r in conn.execute(
                "SELECT DISTINCT tender_category FROM tenders "
                "WHERE tender_category IS NOT NULL AND tender_category<>'' "
                "ORDER BY tender_category")],
            "tender_types": distinct_values(conn, "tender_type"),
            "product_categories": distinct_values(conn, "product_category"),
            "forms_of_contract": raw_json_options(conn, "form_of_contract"),
            "payment_modes": raw_json_options(conn, "payment_mode"),
        }
    return cached("options", OPTIONS_TTL, build)


@app.get("/browse", response_class=HTMLResponse)
def browse(request: Request, q: str = "", scope: str = "all",
           org: list[str] = Query(default=[]),
           date_from: str = "", date_to: str = "",
           category: list[str] = Query(default=[]),
           captured: str = "",
           tender_type: list[str] = Query(default=[]),
           product_category: list[str] = Query(default=[]),
           tender_id: str = "", ref_number: str = "", pincode: str = "",
           value_min: str = "", value_max: str = "",
           form_of_contract: list[str] = Query(default=[]),
           payment_mode: list[str] = Query(default=[]),
           criteria: list[str] = Query(default=[]),
           page: int = Query(1, ge=1), partial: int = 0):
    """Search & browse: a keyword search with an expandable Advanced panel.

    The six dropdown filters are repeatable: ``?org=X&org=Y`` means either. A
    single ``?org=X`` — every link this site has ever emitted, and every one
    anybody has bookmarked — is the one-element case and behaves as before.

    ``partial=1`` returns only the tender-card fragment for one page (used by the
    infinite-scroll loader)."""
    match = build_match(q)
    offset = (page - 1) * PAGE_SIZE
    # An empty string arrives from a dropdown left on "Any"; carrying it through
    # would put `org=` in every share link and make `advanced_open` true forever.
    def picked(values: list[str]) -> list[str]:
        return [v for v in values if v.strip()]

    filt = dict(org=picked(org), date_from=date_from, date_to=date_to,
                category=picked(category),
                captured=captured, tender_type=picked(tender_type),
                product_category=picked(product_category), tender_id=tender_id,
                ref_number=ref_number, pincode=pincode, value_min=value_min,
                value_max=value_max, form_of_contract=picked(form_of_contract),
                payment_mode=picked(payment_mode))
    criteria = [c for c in criteria if c in CRITERIA]
    advanced_open = bool(any(filt.values()) or criteria or scope != "all")
    conn = _reader()
    tenders, total_t = search_tenders_advanced(
        conn, match, limit=PAGE_SIZE, offset=offset, criteria=tuple(criteria), **filt)

    if partial:
        return templates.TemplateResponse(
            request, "_tender_cards.html", {"tenders": tenders})

    pag_t = _pagination(total_t, page, PAGE_SIZE)
    docs = None
    pag_d = None
    if scope in ("all", "docs") and match:
        docs, total_d = search_documents(conn, match, PAGE_SIZE, offset,
                                         parse_terms(q))
        pag_d = _pagination(total_d, page, PAGE_SIZE)
    # doseq: the list-valued filters expand to repeated params, which is what
    # every link on the page (pager, infinite scroll, share) has to round-trip.
    qs = urlencode([("q", q), ("scope", scope), *filt.items(),
                    *[("criteria", c) for c in criteria]], doseq=True)
    ctx = {"q": q, "scope": scope, "match": match,
           "criteria_defs": CRITERIA, "criteria": set(criteria),
           "advanced_open": advanced_open,
           # Drives autofocus: a page that already carries an answer must not
           # steal focus back to the question.
           "searched": bool(q or advanced_open),
           "tenders": tenders, "docs": docs, "pag_t": pag_t, "pag_d": pag_d,
           "qs": qs, "page_size": PAGE_SIZE, "total_t": total_t,
           "stats": _stats()}
    ctx.update(_options())
    ctx.update(filt)
    return templates.TemplateResponse(request, "browse.html", ctx)


@app.get("/search")
def search_redirect(request: Request):
    """Back-compat: old /search links now live under /browse."""
    qs = request.url.query
    return RedirectResponse("/browse" + (f"?{qs}" if qs else ""), status_code=307)


def _corrigenda(tender: dict) -> list[dict]:
    """The tender's amendment history, newest last, as the portal listed it.

    Only entries carrying a type are kept: the ``title`` is the department's own
    free text and is often uninformative on its own ("Corrigendum 9"), while the
    type is the portal's own classification and is what makes the row mean
    something.
    """
    try:
        entries = json.loads(tender.get("corrigenda_json") or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(entries, list):
        return []
    return [{"title": (e.get("title") or "").strip(),
             "type": (e.get("type") or "").strip()}
            for e in entries if isinstance(e, dict) and (e.get("type") or e.get("title"))]


@app.get("/tender/{tender_id}", response_class=HTMLResponse)
def tender_detail(request: Request, tender_id: str):
    conn = _reader()
    row = conn.execute("SELECT * FROM tenders WHERE tender_id=?", (tender_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Tender not found")
    tender = dict(row)
    try:
        tender["raw"] = json.loads(tender.get("raw_json") or "{}")
    except json.JSONDecodeError:
        tender["raw"] = {}
    docs = [dict(d) for d in conn.execute(
        "SELECT * FROM documents WHERE tender_id=? ORDER BY section, filename",
        (tender_id,))]
    # One query for the whole tender rather than db.document_versions per row:
    # superseded copies are rare, and a page with 17 documents should not cost 17
    # round trips to discover that 16 of them have never changed.
    versions: dict[int, list[dict]] = {}
    for v in conn.execute(
            "SELECT * FROM document_versions WHERE tender_id=?"
            " ORDER BY superseded_at DESC, id DESC", (tender_id,)):
        versions.setdefault(v["document_id"], []).append(dict(v))
    for d in docs:
        d["kind"] = preview_kind(d["filename"])
        d["versions"] = versions.get(d["id"], [])
    flag_hours = short_window(tender.get("published_date"), tender.get("closing_date"),
                              tender.get("raw"))
    # Computed live for the same reason flag_hours is: a tender re-typed on the
    # portal should stop or start carrying the mark on its next page load, not
    # wait for a backfill.
    limited_kind = limited_tender(tender.get("tender_type"))
    return templates.TemplateResponse(
        request, "tender.html",
        {"t": tender, "docs": docs, "flag_hours": flag_hours,
         "limited_kind": limited_kind,
         "corrigenda": _corrigenda(tender), "award": award_panel(tender, docs),
         # Computed here and not in the template: it reads raw_json, which this
         # handler has already parsed, and no list page may ever import it.
         "crit_dates": critical_dates(tender),
         "og": og_meta(tender, flag_hours, len(docs))},
    )


def _doc_row(document_id: int):
    row = _reader().execute(
        "SELECT id, tender_id, filename, stored_path, status, content_type "
        "FROM documents WHERE id=?", (document_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Document not found")
    return row


@app.get("/doc/{document_id}")
def download_doc(document_id: int, inline: int = 0, version: int = 0):
    """The captured file, or with ``version`` a superseded copy of it.

    Serving old versions is the point of keeping them: an archive that can only
    hand over the newest specification cannot show that the specification
    changed.
    """
    row = _doc_row(document_id)
    if version:
        old = _reader().execute(
            "SELECT filename, stored_path, content_type FROM document_versions"
            " WHERE id=? AND document_id=?", (version, document_id)).fetchone()
        if not old or not old["stored_path"]:
            raise HTTPException(404, "Version not found")
        row = old
    elif row["status"] != "captured" or not row["stored_path"]:
        raise HTTPException(410, "Document was deleted by the source before it could be captured")
    path = PROJECT_ROOT / row["stored_path"]
    if not path.exists():
        raise HTTPException(410, "File missing on disk")
    ext = Path(row["filename"]).suffix.lower()
    media = (row["content_type"] if not inline else None) or INLINE_MEDIA.get(ext) \
        or row["content_type"] or "application/octet-stream"
    return FileResponse(
        path, filename=row["filename"], media_type=media,
        content_disposition_type="inline" if inline else "attachment")


@app.get("/view/{document_id}", response_class=HTMLResponse)
def view_doc(request: Request, document_id: int, partial: int = 0):
    """In-app document viewer.

    ``partial=1`` returns just the preview body, which the in-page modal fetches;
    the full page is the no-JavaScript fallback and renders the same fragment."""
    row = _doc_row(document_id)
    path = PROJECT_ROOT / row["stored_path"] if row["stored_path"] else None
    captured = row["status"] == "captured" and path is not None and path.exists()
    view = build_preview(path, row["filename"]) if captured \
        else {"kind": preview_kind(row["filename"]), "sheets": [], "text": "", "error": ""}
    ctx = {"doc": dict(row), "view": view, "captured": captured}
    return templates.TemplateResponse(
        request, "_docview.html" if partial else "viewer.html", ctx)


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """Serve the service worker from root so its scope covers the whole site."""
    sw = (HERE / "static" / "sw.js").read_text()
    return PlainTextResponse(sw, media_type="application/javascript",
                             headers={"Service-Worker-Allowed": "/"})


@app.get("/credits", response_class=HTMLResponse)
def credits(request: Request):
    """Artwork attributions — kept off the footer so it stays a single line."""
    return templates.TemplateResponse(request, "credits.html", {})


@app.get("/bookmarks", response_class=HTMLResponse)
def bookmarks(request: Request):
    """Saved tenders — rendered client-side from localStorage."""
    return templates.TemplateResponse(request, "bookmarks.html", {})


@app.get("/watches")
def watches_redirect():
    """Saved searches live on /bookmarks, which is the header's one Saved entry.

    They were briefly a page of their own. They should not be: a saved search
    and a saved tender are both "things I kept", people look for them in the
    same place, and a second list behind a second icon is a list nobody finds.
    Kept as a redirect because notifications sent before the move point here.
    """
    return RedirectResponse("/bookmarks#searches", status_code=307)


def _doc_count(tender_id: str) -> int:
    return _reader().execute("SELECT count(*) FROM documents WHERE tender_id=?",
                         (tender_id,)).fetchone()[0]


@app.get("/og/{tender_id}.png", include_in_schema=False)
def og_image(tender_id: str):
    """Per-tender Open Graph share card (generated on demand, cached on disk).

    The cached PNG carries the token it was drawn from, so a corrigendum or a
    bumped render version regenerates it without anyone emptying the cache."""
    row = _reader().execute("SELECT * FROM tenders WHERE tender_id=?", (tender_id,)).fetchone()
    if not row:
        return FileResponse(HERE / "static" / "og.png", media_type="image/png")
    tender = dict(row)
    flag_hours = short_window(tender.get("published_date"), tender.get("closing_date"),
                              json.loads(tender.get("raw_json") or "{}"))
    out = og_path(cfg, tender_id)
    token = og_token(tender, flag_hours)
    if not is_fresh(out, token):
        render_og(tender, out, flag_hours, _doc_count(tender_id))
    return FileResponse(out, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
def healthz():
    return JSONResponse(_stats())
