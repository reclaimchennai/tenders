# TN Tenders Mirror & Archive

An independent, public-interest archive of Tamil Nadu government e-procurement
tenders (the NIC **GePNIC** portal at `tntenders.gov.in`).

The official portal **deletes tender documents once a tender closes**, which
makes it hard for journalists and activists to scrutinise the scope and
requirements of past procurement. This project:

1. **Captures documents while tenders are still active** (the official site
   removes them after close — once gone, they are unrecoverable).
2. **Mirrors all tender metadata** into a local database, including tenders whose
   documents are already lost.
3. **Full-text indexes the contents of every captured document** (PDF text +
   OCR for scans, XLS/XLSX), so you can search inside the requirements.
4. Serves a simple **searchable mirror website** with downloads.

## How the portal works (what the scraper relies on)

Hands-on findings that shape the design:

| Aspect | Finding |
| --- | --- |
| Tender detail permalinks | `…&page=FrontEndViewTender&…&sp=<token>` are **stable** over time (verified 10 months later). A `session=T` variant is session-bound; we strip it. |
| Document deletion | After close, filenames render as plain text — **the files are gone**. |
| Listing search | The keyword search is **captcha-gated**. |
| Enumeration | **"Tenders by Organisation"** is an org tree whose drill-down links are plain GET links — **no captcha**. Walking it yields every active tender. |
| Document download | Gated by a **per-tender image captcha** (`DocDownCaptcha`). After a correct captcha the page re-renders each document as a tokenised `DirectLink` that streams the file. Solved by a locally-trained CNN — see below. |

## Install

```bash
# Debian / Ubuntu. fonts-dejavu-core is not optional: the synthetic captcha
# generator reconstructs the portal's own font, and without it a fresh install
# cannot train a solver from scratch.
sudo apt install tesseract-ocr poppler-utils fonts-dejavu-core
# macOS: brew install tesseract poppler font-dejavu

python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # add ".[ml]" for the trained captcha solver
```

Or run the whole thing in a container — see
[docs/SELF-HOSTING.md](docs/SELF-HOSTING.md).

Everything runs locally. There is no API key to obtain, no hosted model, and no
account to create: the only host this software talks to is the tender portal
itself (plus your browser's push service, if you turn notifications on).

## Usage

```bash
# 1. Seed metadata from your existing CSV export (one-time).
tenders-import "/path/to/all_detailed_tenders.csv"

# 2. Discover active tenders (captcha-free org-tree walk).
tenders-backfill --listing active

# 3. Fetch detail pages + download live documents (solves captchas).
tenders-detail --download --limit 200

# 4. Extract text (+OCR) from captured documents and refresh the index.
tenders-extract

# 5. Run the mirror website.
tenders-web                # http://127.0.0.1:8000

# Daily forward capture (does 2–4 for currently-active tenders):
tenders-forward

# Anytime:
tenders-stats              # counts of tenders / captured vs lost docs
tenders-index              # rebuild FTS indexes
```

### Document-download captcha — a solver you train yourself

Document downloads are gated by a per-tender image captcha. **Nothing in this
project sends a captcha to an outside service to be read.** That is a hard
constraint, not a preference: an archive that depends on somebody else's API to
function can be switched off by that somebody, and this one exists precisely
because the material it preserves is inconvenient to powerful people.

Tesseract manages only ~15% here — the portal stamps solid black squares over
the glyphs and they fuse with the letters — so the real solver is a small CNN
trained locally. Measured against every captcha the portal itself confirmed an
answer for, it reads **1,640 of 1,640**.

You do not need any labelled data to get there. `captcha_synth.py` reconstructs
the portal's own generator (DejaVu Sans at 38px, matched by ink-coverage
fingerprinting), so a fresh checkout trains from synthetic images alone:

```bash
pip install -e ".[ml]"      # PyTorch, CPU-only is fine
tenders-captcha-corpus      # render a synthetic corpus (offline, no portal traffic)
tenders-captcha-train       # trains data/captcha/model.pt
```

Once `data/captcha/model.pt` exists, `solve_image` uses it automatically. Until
then downloads still work, slowly, via Tesseract; setting `captcha_manual = true`
in `config.toml` lets you type them yourself instead.

Optionally, to fine-tune on real captchas rather than synthetic ones:

```bash
tenders-captcha-collect --target 500   # gather raw captchas (polite, rate-limited;
                                       # only visits tenders that already have a
                                       # live download gate)
tenders-captcha-label                  # http://127.0.0.1:8001 — each is pre-filled
                                       # with a Tesseract guess; glance, fix, Enter
tenders-captcha-train
```

Every correct solve the portal accepts in normal operation is also saved as a
verified label, so the training set grows on its own as the mirror runs.

### Running it continuously

`deploy/` holds systemd **user** units (no root, no system-wide install) for the
whole set: the capture loop, text extraction, the award sweep, push
notifications, a nightly database-maintenance job, and a watchdog. Install and
enable them:

```bash
cp deploy/*.service deploy/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now tenders-web tenders-scraper tenders-extract \
    tenders-watch.timer tenders-healthcheck.timer tenders-dbmaint.timer
loginctl enable-linger "$USER"      # so they survive logout and reboot
```

Two of those exist because of failures that actually happened:

- **`tenders-healthcheck.timer`** probes the site every two minutes and restarts
  it if it has wedged. On 2026-08-15 the web process stopped answering entirely
  while still looking healthy to systemd — the port accepted connections and
  returned nothing — so a liveness check has to require a complete response, not
  a successful connect.
- **`tenders-dbmaint.timer`** truncates the SQLite WAL nightly. An unchecked WAL
  reached **44 GB against a 1.9 GB database** and took the site down with it;
  SQLite reuses a WAL but never shrinks one on its own.

Prefer containers? See [docs/SELF-HOSTING.md](docs/SELF-HOSTING.md).

### Backups

```bash
scripts/backup.sh /Volumes/Backup/tenders   # DB snapshot + docs + raw HTML
```

## Architecture

```
src/tenders/
  config.py            config.toml loader + path resolution
  db.py                SQLite schema (tenders, documents, doc_text, FTS5, …)
  util.py              date / money / whitespace normalisation
  http_client.py       polite session: rate-limit, retries, request cap
  csv_import.py        seed metadata from the CSV export
  parse_listing.py     org-tree + tender-list parsers
  enumerate_listings.py captcha-free org-tree walk (resumable, idempotent)
  parse_detail.py      73-field detail parser + live-vs-deleted doc detection
  jsf.py               GePNIC stateful-form field extraction
  captcha.py           decode + denoise; local CNN solve (Tesseract/manual fallback)
  captcha_model.py     the trained CTC network and its loader
  captcha_synth.py     reconstructs the portal's generator — train with no labels
  download_docs.py     captcha-gated document download (stream, dedup, hash)
  pipeline.py          fetch → archive HTML → parse → store (→ download)
  extract_text.py      PDF text-layer → OCR fallback; XLS/XLSX
  index_fts.py         (re)build FTS5 search indexes
  forward_capture.py   the daily job: enumerate → detail → download → index
  capture_retry.py     progressive re-probe (1m/5m/15m/30m/60m) for new tenders
  latest_active.py     newest-first poll — catches same-day open-and-close tenders
  redflags.py          short-bidding-window detection
  watches.py           saved searches, bookmark alerts, Web Push
  stats.py             summary counts
  web/                 FastAPI + Jinja2 mirror site
```

The **SQLite database is the single source of truth and the work queue**: tenders
advance `discovered → detailed → failed`; documents advance
`pending → captured | lost | failed`. Every stage is idempotent and resumable.
Documents live on disk under `data/docs/<tender_id>/`; raw detail HTML is archived
gzipped under `data/html/<tender_id>/` for provenance.

## Ethics & rate limiting

This archives **public procurement records** for public-interest scrutiny. The
scraper is deliberately polite: single-threaded, multi-second jittered delays
(`config.toml [scrape]`), a per-run request cap kill-switch, and no access to any
authenticated/non-public area. Run large backfills over days, not hours.
