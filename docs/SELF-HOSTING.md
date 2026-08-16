# Self-hosting a TN Tenders mirror

This runs the whole archive — the website, the capture loop, text extraction,
the award sweep and the three maintenance timers — in one container.

Everything in it runs locally. There is no hosted API to sign up for, no model
endpoint, no OCR service. That is not a convenience choice: this archive exists
because the portal deletes tender documents when a tender closes, and an
archive of inconvenient material that depends on somebody else's service can be
switched off by that somebody. The image carries its own OCR engine, its own
PDF rasteriser and its own captcha model — which it can *train from scratch*,
offline, because `captcha_synth.py` reconstructs the portal's own captcha
generator using DejaVu Sans (installed in the image and checked at every boot).

> **Before you start, read [Politeness](#politeness-is-a-safety-property-not-a-setting).**
> This scrapes a live government portal that other people depend on.

---

## Quick start

```bash
git clone <your fork of this repo>
cd tender
docker compose up -d --build          # ~2 minutes; the image is 1.77 GB,
                                      # roughly half of which is the CPU build
                                      # of PyTorch that runs the captcha model
curl -s localhost:8013/healthz        # {"tenders_total":0, ...}
docker compose logs -f
```

That is a complete, empty mirror. It has created its data volume, initialised
the database, started the website on port 8013 and begun walking the portal's
organisation tree for currently-active tenders. Documents start landing in
`data/docs/` within the first cycle.

To watch what it is doing:

```bash
docker compose exec tenders supervisorctl -c /app/deploy/supervisord.conf status
docker compose exec tenders tenders-stats
```

To stop it:

```bash
docker compose down          # stops the mirror, KEEPS the archive
docker compose down -v       # DELETES THE ARCHIVE. See "Backups" first.
```

`down -v` removes the volume, and the volume holds documents the portal itself
has already deleted. There is no copy of those anywhere else unless you made
one.

---

## What is actually running

Eight processes under one supervisor. Each replicates a systemd unit from
`deploy/`, and those unit files are still the place to read *why* each one
behaves the way it does — they carry the measurements and the incident history.

| Process | Replicates | What it does |
| --- | --- | --- |
| `web` | `tenders-web.service` | FastAPI/uvicorn site on port 8013 |
| `scraper` | `tenders-scraper.service` | continuous capture: enumerate → detail → download |
| `extract` | `tenders-extract.service` | PDF text + OCR → full-text index |
| `award-sweep` | `tenders-award-sweep.service` | retrospective Award-of-Contract sweep; exits when done |
| `watch-timer` | `tenders-watch.timer` | every 5 min: saved-search / bookmark push notifications |
| `healthcheck-timer` | `tenders-healthcheck.timer` | every 2 min: **the watchdog** (see below) |
| `dbmaint-timer` | `tenders-dbmaint.timer` | daily 04:30: WAL guard + integrity check |
| `captcha-train-timer` | `tenders-captcha-train.timer` | daily 03:15: retrain the captcha CNN |

Two of them are not optional in any meaningful sense.

**The watchdog** exists because of a specific failure. On 2026-08-15 the web
process stopped serving without dying: the process was alive, systemd said
"active (running)", the listening socket kept completing TCP handshakes, and it
never wrote another byte of any response. Every automatic defence in place
missed it, because they all asked "did the process exit?" and the answer was
no. So the watchdog requires a **complete, valid HTTP response body inside a
hard deadline** — three failures, five seconds apart, before it acts, and never
more than three restarts per hour, after which it stops acting and starts
shouting, because if restarting has not fixed it in three tries the fault is
somewhere else. In this container it restarts the *web process*, not the
container.

**The WAL guard** exists because the write-ahead log of a 1.9 GB database once
reached **44 GB** and took the site down. A passive SQLite checkpoint rewinds
the WAL to reuse the space but never shortens the file, and it gives up
silently whenever a reader holds an old snapshot — so a WAL that spikes once
stays that size on disk forever. `PRAGMA wal_checkpoint(TRUNCATE)` is the only
thing that returns the space, and that is this job's entire purpose. It never
repairs, vacuums or deletes: if the integrity check fails it stops, logs
EMERGENCY and leaves every byte where it is for a human, because an automatic
"fix" would overwrite the only surviving copy of the evidence.

Leave both switched on.

---

## Ports

| Port | What |
| --- | --- |
| 8013 | the mirror website (and `/healthz`) |

The compose file publishes it on `127.0.0.1` only. The site has no
authentication — it serves a public archive, so there is nothing to
authenticate — but that is a different decision from "reachable from the whole
internet over plain HTTP". Put a TLS-terminating reverse proxy in front of it
and let that be the only thing listening publicly.

Two separate knobs, deliberately:

* `TENDERS_PUBLISH_PORT` — the port on your host. Change this to move the site.
* `TENDERS_WEB_PORT` — the port *inside* the container. Both the frontend and
  the watchdog read it, so they cannot drift apart. You almost never need to
  touch this.

If they did drift, the watchdog would probe a port nothing listens on, conclude
the site was down, and restart a perfectly healthy service every two minutes
forever. (You will see one informational line per watchdog run saying
`config.toml [web].port=8000 but the unit binds 8013`. That is expected: the
command line wins over the config value, and the watchdog reports the
difference rather than acting on it.)

---

## Environment variables

All optional. Set them in `docker-compose.yml`, or in a `.env` file next to it.

| Variable | Default | What it does |
| --- | --- | --- |
| `TENDERS_PUBLISH_PORT` | `8013` | host port the site is published on |
| `TENDERS_WEB_PORT` | `8013` | port inside the container (frontend **and** watchdog) |
| `TZ` | `Asia/Kolkata` | the daily jobs fire at wall-clock times; set your zone |
| `TENDERS_UID` / `TENDERS_GID` | `1001` | build-time uid/gid — see [Permissions](#permissions) |
| `TENDERS_MEM_LIMIT` | `6g` | container memory ceiling |
| **What runs** | | all default to `true` |
| `TENDERS_ENABLE_WEB` | `true` | the website |
| `TENDERS_ENABLE_SCRAPER` | `true` | **the only thing that talks to the portal**, with the award sweep |
| `TENDERS_ENABLE_EXTRACT` | `true` | text extraction + indexing (local CPU only) |
| `TENDERS_ENABLE_AWARD_SWEEP` | `true` | award-of-contract sweep (talks to the portal) |
| `TENDERS_ENABLE_WATCH` | `true` | push notifications (no-op without a VAPID key) |
| `TENDERS_ENABLE_HEALTHCHECK` | `true` | the watchdog. Leave it on. |
| `TENDERS_ENABLE_DBMAINT` | `true` | the WAL guard. Leave it on. |
| `TENDERS_ENABLE_CAPTCHA_TRAIN` | `true` | nightly captcha retrain |
| **Pacing** | | see the politeness section |
| `TENDERS_CYCLE_PAUSE` | `900` | seconds between capture cycles |
| `TENDERS_CANCELLED_EVERY` | `6` | cancelled/retendered sweep every Nth cycle |
| **Maintenance** | | |
| `TENDERS_DBMAINT_AT` | `04:30` | daily WAL guard time |
| `TENDERS_CAPTCHA_TRAIN_AT` | `03:15` | daily retrain time |
| `TENDERS_WAL_MAX_MB` | `1024` | WAL size past which it is truncated |
| **Advanced** | | read `scripts/healthcheck.py` first |
| `TENDERS_CONFIG` | `/app/config.toml` | path to an alternative config file |
| `TENDERS_HEALTHCHECK_URL` | — | full override for the watchdog's probe URL |
| `TENDERS_HEALTHCHECK_PATH` | `/healthz` | probe path |
| `TENDERS_HEALTHCHECK_STATE` | `data/healthcheck_state.json` | the restart-storm ledger |

Everything else lives in `config.toml`, which is committed and holds no secrets
— paths, tuning, rate limits. To change it without rebuilding, uncomment the
`./config.toml:/app/config.toml:ro` line in `docker-compose.yml`.

### Running the mirror read-only

```bash
TENDERS_ENABLE_SCRAPER=false TENDERS_ENABLE_AWARD_SWEEP=false docker compose up -d
```

With those two off, **nothing in the container sends a single request to the
portal**. Everything else still works: the site serves, search works, text
extraction of already-captured documents continues, the watchdog and the WAL
guard keep running. This is the right mode for serving an archive somebody else
captured, and for testing changes.

---

## The data volume

Everything mutable lives in one place, mounted at `/app/data`:

| Path | What | Size on the reference deployment |
| --- | --- | --- |
| `tenders.db` (+ `-wal`, `-shm`) | the database — metadata **and the work queue** | 1.9 GB |
| `docs/<tender_id>/` | captured documents. **This is the archive.** | 69 GB |
| `html/<tender_id>/` | gzipped detail pages, for provenance | 1.3 GB |
| `captcha/` | trained model, labels, synthetic corpus | 1.3 GB |
| `secrets/vapid_private.pem` | the Web Push identity (0600 in a 0700 dir) | — |
| `backups/` | database snapshots taken before schema changes | 2.5 GB |
| `state/` | timer stamps, so the daily jobs survive a rebuild | — |
| `healthcheck_state.json` | the watchdog's restart ledger | — |

By default this is a Docker named volume called `tenders-data`. A named volume
rather than `./data:/app/data` on purpose: a relative bind mount is silently
created as an empty directory if the path is wrong or compose is run from
another directory, and the container then starts a *fresh, empty* mirror while
the real archive sits untouched and unserved next door.

For 69 GB you probably want to choose the disk. Replace the volume line in
`docker-compose.yml` with an absolute path and read [Permissions](#permissions):

```yaml
    volumes:
      - /srv/tenders/data:/app/data
```

A first run against an empty volume works and is expected: the image creates
the whole tree itself (`config.ensure_dirs()` plus `secrets/` and `state/`) and
the database is created on the frontend's first import.

---

## Permissions

**This is the most common self-hosting failure.** A bind-mounted host directory
owned by a different uid than the container's user, and nothing can write to
the archive.

With the default named volume there is nothing to do — Docker seeds an empty
volume from the image, ownership included.

With a bind mount, make the uids match. Either chown the host directory:

```bash
sudo chown -R 1001:1001 /srv/tenders/data
```

or build the image for your own uid:

```bash
docker compose build --build-arg TENDERS_UID=$(id -u) --build-arg TENDERS_GID=$(id -g)
```

The container checks this at startup and refuses to start with the exact
command you need to run, rather than failing five seconds later with a
`sqlite3` error that names a permission problem without naming the fix.

It does **not** silently `chown -R` your archive for you. That would mean
walking 39,000 directories and 69 GB of irreplaceable files on every container
start, and re-owning an archive is a decision a human should make with a
command they typed themselves.

Everything runs as uid 1001, never root.

---

## Getting a captcha model

Document downloads are gated by a per-tender image captcha. Tesseract reads
these only ~15% of the time — the portal draws ~30 solid black squares over the
text and they fuse with the glyphs — so the real solver is a small CNN trained
from `data/captcha/model.pt`.

**A fresh mirror has no model, and that is fine.** Downloads still work: the
solver falls back to Tesseract, `config.toml` sets `captcha_attempts = 15`, and
each retry is an independent fresh captcha, so 15 attempts at ~15% succeeds
more often than not. It is slow, not broken.

**Every captcha the portal accepts is automatically saved as a verified
training label.** So a running mirror accumulates its own training set with no
manual effort, and the nightly retrain picks it up.

The path from nothing to a working model:

1. **Just run the mirror.** Verified labels accumulate as documents download.
2. **After 30 labels have accumulated**, the nightly `tenders-captcha-train`
   starts doing real work. Check on it:
   ```bash
   docker compose exec tenders python -c "
   import json; from tenders.config import load_config
   from tenders.captcha_model import labels_path
   print(len(json.loads(labels_path(load_config()).read_text())), 'labels')"
   ```
   Below 30 it prints `need >=30 labelled captchas, have N` and exits quietly.
   **This floor is real and there is currently no way around it**: the trainer
   requires 30 hand-or-portal-verified labels before it will run at all, even
   though almost all of its training signal then comes from synthetic data.
3. **The synthetic data is what makes 30 labels enough.**
   `captcha_synth.py` reconstructs the portal's generator — DejaVu Sans at
   38px, identified by scoring rendered glyphs against per-class medians from
   real captchas (0.81 IoU against 0.70 for the runner-up), with the noise
   model fitted to measured block counts. Training then draws thousands of
   exactly-labelled synthetic samples per epoch and uses the real ones only to
   close the gap. **This needs no network and no external service** — you can
   pre-render a corpus with the container fully offline:
   ```bash
   docker compose exec tenders tenders-captcha-corpus --train 400000 --val 25000 --test 25000
   ```
4. **To skip the wait**, collect and label captchas yourself. This is the
   documented route in the README and it is polite — it only visits tenders
   that are already active and rate-limits like everything else:
   ```bash
   docker compose exec tenders tenders-captcha-collect --target 500
   docker compose exec tenders tenders-captcha-label     # web UI on :8001
   docker compose exec tenders tenders-captcha-train
   ```
   The labelling UI defaults to `127.0.0.1:8001`, which inside a container
   means "reachable from inside this container only". To reach it from your
   browser, publish the port *and* tell it to bind the container's external
   interface:

   ```bash
   docker compose run --rm -p 127.0.0.1:8001:8001 tenders \
     tenders-captcha-label --host 0.0.0.0 --port 8001
   ```
5. **Or copy a model in.** `data/captcha/model.pt` is a plain file. If you
   already have a trained one, drop it into the volume and restart.

The nightly retrain is safe to leave on. It no-ops unless 10 new verified
labels have arrived, and it only replaces the deployed model if the candidate
beats it on the same validation split — a bad night cannot make the live solver
worse.

The one thing that would break all of this is DejaVu Sans going missing from
the image. Without it the generator cannot run, the CNN can never be trained,
and the mirror is stuck on the 15% fallback forever. The container checks for
it at every boot and prints either
`captcha_synth: ok — DejaVu Sans found, sample (187, 45) 'UzX3Jd'`
or a loud warning.

---

## Web push (optional)

Saved-search and bookmark notifications need a VAPID keypair. **A mirror
without one still serves the entire archive** — every push-facing endpoint
answers "not available here" and the UI hides the control.

```bash
docker compose exec tenders tenders-push-keys      # writes data/secrets/vapid_private.pem
docker compose restart
```

The key is a file at a path named in `config.toml`, never a value in the config
file. Back it up **out of band** — it is not in any database backup, and
rotating it invalidates every subscription anyone has already created, because
browsers pin the public half into the subscription itself.

---

## Checking it is healthy

```bash
docker compose ps                       # STATUS should say (healthy)
curl -s localhost:8013/healthz | jq
docker compose exec tenders supervisorctl -c /app/deploy/supervisord.conf status
docker compose exec tenders tenders-stats
```

A healthy `supervisorctl status` looks like this:

```
award-sweep                      EXITED    Aug 16 07:12 AM
captcha-train-timer              RUNNING   pid 46, uptime 0:05:51
dbmaint-timer                    RUNNING   pid 44, uptime 0:05:51
extract                          RUNNING   pid 41, uptime 0:05:51
healthcheck-timer                RUNNING   pid 43, uptime 0:05:51
scraper                          RUNNING   pid 39, uptime 0:05:51
watch-timer                      RUNNING   pid 42, uptime 0:05:51
web                              RUNNING   pid 40, uptime 0:05:51
```

**`award-sweep` showing `EXITED` is normal and correct.** It is a job that
finishes: it works through the queue of awarded tenders whose
Award-of-Contract PDF has not been fetched, and then stops. On a new mirror the
queue is empty and it exits within a second. It will have work again after the
scraper has discovered awarded tenders; restart it whenever you want with
`supervisorctl restart award-sweep`. A sweep that had actually failed says so —
it retries three minutes apart, up to ten times, and then logs
`EMERGENCY: failed 10 times`.

`/healthz` returning 200 is **not** sufficient proof of health, and this is not
a theoretical concern — it is exactly what the 2026-08-15 outage looked like
from the outside. The container's own `HEALTHCHECK` therefore applies the same
standard the watchdog does: a complete HTTP 200, at least 16 bytes of body,
parsing as JSON with at least one field, delivered inside a hard 10-second
wall-clock deadline. You can run that check yourself:

```bash
docker compose exec tenders docker-entrypoint.sh probe
# http://127.0.0.1:8013/healthz -> status=200 bytes=231 elapsed=0.05s valid JSON, 11 fields, 231 bytes
```

A wedged frontend prints
`no complete response within 10s — this is the wedge signature (socket accepted, nothing served)`
and, two minutes later at the latest, the watchdog restarts the web process and
logs `RECOVERED`. The container stays up throughout; only the web process is
replaced.

If you see the watchdog log `EMERGENCY … NOT restarting again`, it has hit the
three-restarts-per-hour guard. Restarting is not fixing the problem and
something else is wrong — check `docker compose logs` for the extract loop and
check the WAL size:

```bash
docker compose exec tenders python /app/scripts/db_maint.py
```

---

## Backups

The database and the documents need different treatment, and the difference
matters.

```bash
# 1. The database. `docker cp` or `cp` of a live WAL database races the scraper
#    and yields a file whose WAL and main file disagree. `.backup` takes a read
#    transaction and produces a consistent point-in-time image while the mirror
#    keeps running.
docker compose exec tenders sqlite3 /app/data/tenders.db \
  ".backup '/app/data/backups/tenders-$(date -u +%Y%m%dT%H%M%SZ).db'"

# 2. Everything else — documents, raw HTML, the captcha model, the push key.
docker run --rm -v tenders-data:/data:ro -v "$PWD":/out alpine \
  tar czf /out/tenders-archive-$(date -u +%Y%m%d).tar.gz \
      -C /data docs html captcha secrets
```

The repo's own `scripts/backup.sh` does both of those in one step and works
inside the container — `sqlite3` and `rsync` are installed for exactly this
reason. Invoke it through `bash`: the file is committed non-executable (mode
644), so calling it by path fails with `Permission denied` in the container and
on a host checkout alike.

```bash
docker compose exec tenders bash scripts/backup.sh /app/data/backups
```

Note that it writes into the volume, so it protects you from a corrupted
database, not from a lost disk. Point it at a mount from somewhere else for a
real backup.

What to keep, in order of how irreplaceable it is:

1. **`docs/`** — captured documents. The portal has already deleted many of
   these. They cannot be re-fetched by anyone, ever.
2. **`tenders.db`** — metadata for tenders whose documents are already lost,
   plus every extracted document text and the search index.
3. **`secrets/vapid_private.pem`** — losing it invalidates every push
   subscription.
4. `captcha/` — a trained model is ~30 minutes of CPU to rebuild, and the
   labels are worth more than the model.
5. `html/` — provenance. Nice to have, regenerable in principle.

---

## Restoring an existing archive into a fresh container

Say you have a 69 GB `data/` directory from a host install (or a backup) and
want it served by a container.

```bash
# 1. Create the volume without starting anything.
docker compose create

# 2. Copy the archive in. `docker cp` into the volume via a throwaway
#    container, or -- much faster for 69 GB -- find the volume's path on the
#    host and rsync into it:
docker volume inspect tenders-data --format '{{.Mountpoint}}'
sudo rsync -a --info=progress2 /path/to/old/data/ /var/lib/docker/volumes/tenders-data/_data/

# 3. Make it writable by the container's uid.
sudo chown -R 1001:1001 /var/lib/docker/volumes/tenders-data/_data

# 4. Start, and check what arrived.
docker compose up -d
docker compose exec tenders tenders-stats
docker compose exec tenders python /app/scripts/db_maint.py    # quick_check on the restored db
```

Or skip the copy entirely and bind-mount the directory where it already is:

```yaml
    volumes:
      - /home/you/tender/data:/app/data
```

with `TENDERS_UID`/`TENDERS_GID` built to match its owner. This is the option
to prefer for a large existing archive — there is no reason to duplicate 69 GB
to change how it is served.

**Do not point a second mirror at the same `data/` directory.** The database is
the work queue as well as the store; two scrapers sharing it would double the
request rate against the portal, which is the thing this project most wants to
avoid.

Copy the raw database file only when the mirror is stopped. If you must copy it
live, use the `.backup` command above.

---

## Politeness is a safety property, not a setting

**This scrapes a live government portal.** `tntenders.gov.in` is public
infrastructure that other people — bidders, officials, journalists — need to be
working. It is not a service with a paid tier and an SRE team; it is a NIC
GePNIC instance, and it is not hard to hurt.

The defaults in `config.toml` are the safety envelope:

```toml
[scrape]
min_interval_s = 4.0        # minimum seconds between requests
jitter_s = 3.0              # plus 0-3s of random jitter
max_requests_per_run = 0    # per-run kill switch (0 = rely on the above)

[latest]
poll_interval_s = 300       # the fast poll, 2 requests
max_requests_per_hour = 180 # runaway stop for the fast poller
```

That is **one request at a time, single-threaded, with 4-7 seconds between
them**. A full capture cycle takes hours, and that is correct. `README.md` puts
it plainly: run large backfills over days, not hours.

Rules for anyone self-hosting:

* **Never lower `min_interval_s` or `jitter_s`.** They are the only thing
  standing between this software and an accidental denial of service.
* **Never run two mirrors against the portal from the same place.** Rate
  limiting is per-process: two containers is two independent budgets and twice
  the traffic. If you want a copy of the archive, copy the archive — see
  [Restoring](#restoring-an-existing-archive-into-a-fresh-container) — do not
  re-scrape material somebody has already captured.
* **Raising `TENDERS_CYCLE_PAUSE` is always safe. Lowering it is not.** 900
  seconds is the deployed value.
* **Do not add concurrency.** Nothing in this project fetches in parallel, and
  that is a design decision, not an oversight waiting to be optimised.
* **If you are testing**, run with `TENDERS_ENABLE_SCRAPER=false` and
  `TENDERS_ENABLE_AWARD_SWEEP=false`. Then the container touches the portal
  exactly zero times, and everything else still works.

Two processes in this container talk to the portal — `scraper` and
`award-sweep` — and each holds its own rate limiter, so with both running the
combined rate is up to two requests per interval. That matches the reference
deployment. Do not add a third.

Being impolite to the source portal is a real harm, not a hypothetical one. The
archive exists to make public procurement more scrutable; degrading the service
it archives would be a strange way to go about that.

---

## Network access

The container needs to reach exactly one host to do its job:

* `tntenders.gov.in` — the portal.

Optionally, if you have configured Web Push, it also reaches whichever push
service your subscribers' browsers name (`fcm.googleapis.com`,
`updates.push.services.mozilla.com`, …). Those URLs come from the browser
subscriptions in your own database, not from anything hard-coded here.

Nothing else. No model API, no OCR service, no telemetry, no license check, no
package index at runtime. The whole stack starts and serves with
`--network none`; the captcha generator and the CNN trainer run offline too.
If you want to prove that to yourself, run it with the scraper off and no
network at all:

```bash
docker run --rm --network none -v tenders-data:/app/data \
  -e TENDERS_ENABLE_SCRAPER=false -e TENDERS_ENABLE_AWARD_SWEEP=false \
  tenders-mirror:latest
```

---

## Ethics

This archives **public procurement records** for public-interest scrutiny. The
scraper accesses only public, unauthenticated pages, at a deliberately slow
rate, and captures documents that the portal publishes and then deletes. If you
run a mirror, run it in that spirit.
