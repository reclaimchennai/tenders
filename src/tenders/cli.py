"""Command-line entrypoints.

Each function is a console-script target declared in pyproject.toml. Heavy
modules are imported lazily so that, e.g., running the importer does not require
the scraping/OCR stack to be present.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .config import load_config, setup_logging


def _common_parser(desc: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=desc)
    p.add_argument("--db", type=Path, default=None, help="override DB path")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def import_csv_cmd() -> None:
    p = _common_parser("Import the seed CSV into the mirror DB")
    p.add_argument("csv_path", type=Path, help="path to all_detailed_tenders.csv")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .csv_import import import_csv

    result = import_csv(args.csv_path, args.db)
    from .index_fts import rebuild_fts

    result.update(rebuild_fts(args.db))
    print(json.dumps(result, indent=2))


def detail_cmd() -> None:
    p = _common_parser("Fetch + parse tender detail pages (status='discovered')")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--tender-id", default=None, help="process a single tender id")
    p.add_argument("--download", action="store_true", help="also download live docs")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .pipeline import run_detail

    print(json.dumps(run_detail(args.db, limit=args.limit,
                                tender_id=args.tender_id,
                                download=args.download), indent=2))


def backfill_cmd() -> None:
    p = _common_parser("Resumable enumeration of active + archive listings")
    p.add_argument("--listing", choices=["active", "archive", "both"], default="both")
    p.add_argument("--max-pages", type=int, default=0, help="0 = until exhausted")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .enumerate_listings import run_backfill

    print(json.dumps(run_backfill(args.db, listing=args.listing,
                                  max_pages=args.max_pages), indent=2))


def forward_cmd() -> None:
    p = _common_parser("Daily forward capture of active tenders")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .forward_capture import run_forward

    print(json.dumps(run_forward(args.db), indent=2))


def latest_cmd() -> None:
    p = _common_parser("Poll newest-published active tenders (short-window detector)")
    p.add_argument("--max-pages", type=int, default=None,
                   help="pages to walk while rows are still new (0 = unlimited)")
    p.add_argument("--no-capture", action="store_true",
                   help="record metadata only; skip urgent detail/document capture")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .latest_active import run_latest

    print(json.dumps(run_latest(args.db, max_pages=args.max_pages,
                                capture=not args.no_capture), indent=2))


def corrigendum_cmd() -> None:
    p = _common_parser("Sweep active corrigendums and refresh the amended tenders")
    p.add_argument("--max-pages", type=int, default=None,
                   help="0 = the whole list (the default; page 1 is not enough)")
    p.add_argument("--no-capture", action="store_true",
                   help="record metadata only; skip detail re-fetch")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .latest_active import run_latest

    print(json.dumps(run_latest(args.db, max_pages=args.max_pages,
                                capture=not args.no_capture,
                                corrigendums=True), indent=2))


def status_backfill_cmd() -> None:
    p = _common_parser("Backfill historical tenders via the Tenders Status search")
    p.add_argument("--statuses", default="8,7,9",
                   help="comma-separated tenderStatus codes, or 'all' "
                        "(1 To Be Opened, 2 Technical Bid Opening, "
                        "3 Technical Evaluation, 4 Financial Bid Opening, "
                        "5 Financial Evaluation, 6 AOC, 7 Retender, "
                        "8 Cancelled, 9 Concluded)")
    p.add_argument("--start", default=None, help="first closing month, YYYY-MM")
    p.add_argument("--end", default=None, help="last closing month, YYYY-MM")
    p.add_argument("--max-pages", type=int, default=0,
                   help="cap pages per (status, month) window; 0 = exhaust it")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .enumerate_status import STATUSES, run_status

    statuses = (list(STATUSES) if args.statuses.strip().lower() == "all"
                else [s.strip() for s in args.statuses.split(",") if s.strip()])
    unknown = [s for s in statuses if s not in STATUSES]
    if unknown:
        p.error(f"unknown tenderStatus code(s): {', '.join(unknown)}")
    print(json.dumps(run_status(args.db, statuses=statuses, start=args.start,
                                end=args.end,
                                max_pages_per_window=args.max_pages,
                                progress=print), indent=2))


def cancelled_cmd() -> None:
    p = _common_parser("Enumerate + capture Cancelled/Retendered tenders")
    p.add_argument("--no-download", action="store_true",
                   help="record metadata only; skip document download")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .enumerate_cancelled import run_cancelled

    print(json.dumps(run_cancelled(args.db, download=not args.no_download), indent=2))


def shortnames_cmd() -> None:
    p = _common_parser("Generate concise short names for tenders")
    p.add_argument("--limit", type=int, default=200, help="tenders to name this run")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .shortnames import run_shortnames

    print(json.dumps(run_shortnames(args.db, limit=args.limit), indent=2))


def redflags_cmd() -> None:
    p = _common_parser("Scan all tenders and record short-bid-window red flags")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .redflags import scan_all

    print(json.dumps(scan_all(args.db), indent=2))


def unzip_cmd() -> None:
    p = _common_parser("Unpack captured zip bundles into individual documents")
    p.add_argument("--limit", type=int, default=0, help="0 = all captured zips")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .download_docs import explode_existing_zips

    print(json.dumps(explode_existing_zips(args.db, limit=args.limit), indent=2))


def run_cmd() -> None:
    p = _common_parser("Continuous capture daemon: enumerate + download forever")
    p.add_argument("--cycle-pause", type=int, default=900,
                   help="seconds to sleep between cycles (default 900)")
    p.add_argument("--cancelled-every", type=int, default=6,
                   help="run cancelled/retender enumeration every N cycles (0=never)")
    p.add_argument("--once", action="store_true", help="run a single cycle then exit")
    p.add_argument("--verify-batch", type=int, default=500,
                   help="captured documents re-validated per cycle (0=never)")
    p.add_argument("--verify-max-age", type=int, default=12,
                   help="hours a passing validation is trusted for")
    p.add_argument("--latest-interval", type=int, default=None,
                   help="seconds between short-window polls (0 = off, "
                        "default from config [latest].poll_interval_s)")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .continuous import run_continuous

    print(json.dumps(run_continuous(args.db, cycle_pause_s=args.cycle_pause,
                                    cancelled_every=args.cancelled_every,
                                    once=args.once,
                                    verify_batch=args.verify_batch,
                                    verify_max_age_h=args.verify_max_age,
                                    latest_interval_s=args.latest_interval),
                     indent=2))


def verify_cmd() -> None:
    p = _common_parser("Audit captured documents against their stored hashes")
    p.add_argument("--limit", type=int, default=0,
                   help="documents to check (0 = every document due)")
    p.add_argument("--max-age", type=int, default=12,
                   help="skip documents validated within this many hours")
    p.add_argument("--all", action="store_true",
                   help="ignore --max-age and re-check everything")
    p.add_argument("--deep", action="store_true",
                   help="also parse PDFs and CRC-check zips (much slower)")
    p.add_argument("--tender-id", default=None, help="audit one tender only")
    p.add_argument("--dry-run", action="store_true",
                   help="report only; do not re-queue anything")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .integrity import be_nice, run_verify

    # A manual sweep hashes gigabytes; the live scraper must keep priority.
    be_nice()
    print(json.dumps(run_verify(args.db, limit=args.limit,
                                max_age_hours=0 if args.all else args.max_age,
                                deep=args.deep, tender_id=args.tender_id,
                                repair=not args.dry_run,
                                progress=print), indent=2))


def lag_cmd() -> None:
    p = _common_parser("Report how long documents take to appear after publication")
    args = p.parse_args()
    setup_logging(logging.WARNING)
    from .config import load_config
    from .db import connect
    from .doc_lifecycle import lag_report

    conn = connect((args.db or load_config().db_path), read_only=True)
    try:
        print(json.dumps(lag_report(conn), indent=2))
    finally:
        conn.close()


def extract_cmd() -> None:
    p = _common_parser("Extract text (+OCR) from captured documents")
    p.add_argument("--limit", type=int, default=200)
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .extract_text import run_extract

    result = run_extract(args.db, limit=args.limit)
    from .index_fts import rebuild_fts

    result.update(rebuild_fts(args.db))
    print(json.dumps(result, indent=2))


def index_cmd() -> None:
    p = _common_parser("Rebuild FTS indexes")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .index_fts import rebuild_fts

    print(json.dumps(rebuild_fts(args.db), indent=2))


def stats_cmd() -> None:
    p = _common_parser("Print mirror statistics")
    args = p.parse_args()
    setup_logging(logging.WARNING)
    from .stats import gather_stats

    print(json.dumps(gather_stats(args.db), indent=2))


def captcha_collect_cmd() -> None:
    p = _common_parser("Collect raw captcha images for labelling")
    p.add_argument("--target", type=int, default=400)
    p.add_argument("--per-tender", type=int, default=6)
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .captcha_collect import collect

    print(json.dumps(collect(args.db, target=args.target,
                             per_tender=args.per_tender), indent=2))


def captcha_label_cmd() -> None:
    p = _common_parser("Run the captcha labelling web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    args = p.parse_args()
    setup_logging(logging.INFO)
    import uvicorn

    uvicorn.run("tenders.captcha_label:app", host=args.host, port=args.port)



def captcha_train_cmd() -> None:
    p = _common_parser("Train the captcha CNN from labelled images")
    p.add_argument("--epochs", type=int, default=60)
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    cfg = load_config()
    from .captcha_model import train

    print(json.dumps(train(cfg, epochs=args.epochs), indent=2))


def captcha_corpus_cmd() -> None:
    p = _common_parser("Pre-render a synthetic captcha corpus to packed shards")
    p.add_argument("--train", type=int, default=400_000)
    p.add_argument("--val", type=int, default=25_000)
    p.add_argument("--test", type=int, default=25_000)
    p.add_argument("--workers", type=int, default=None)
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    cfg = load_config()
    from .captcha_corpus import build

    print(json.dumps(build(cfg, train=args.train, val=args.val, test=args.test,
                           workers=args.workers), indent=2))


def push_keys_cmd() -> None:
    p = _common_parser("Create the VAPID keypair for Web Push (once per deployment)")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing key — invalidates every "
                        "subscription already created against the old one")
    args = p.parse_args()
    setup_logging(logging.INFO)
    from .push import write_key

    cfg = load_config()
    path, public = write_key(cfg, force=args.force)
    print(json.dumps({"private_key_file": str(path), "public_key": public,
                      "mode": oct(path.stat().st_mode & 0o777)}, indent=2))


def watch_run_cmd() -> None:
    p = _common_parser("Match saved searches and bookmark alerts, then notify")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be sent without sending it")
    args = p.parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from .watches import run_watches

    print(json.dumps(run_watches(args.db, dry_run=args.dry_run), indent=2))


def web_cmd() -> None:
    p = _common_parser("Run the mirror web app")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    args = p.parse_args()
    setup_logging(logging.INFO)
    cfg = load_config()
    import uvicorn

    host = args.host or cfg.web["host"]
    port = args.port or int(cfg.web["port"])
    uvicorn.run("tenders.web.app:app", host=host, port=port, reload=False)
