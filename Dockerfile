# syntax=docker/dockerfile:1.7
#
# TN Tenders Mirror — one image containing the whole mirror: the web frontend,
# the capture loop, the extraction loop, the award sweep, and the three timers
# that keep them honest (watchdog, WAL guard, captcha retrain).
#
# The defining constraint of this project is that everything runs locally. The
# archive exists because the portal deletes tender documents at close, and a
# public-interest archive that needs somebody else's API to function can be
# switched off by that somebody. That constraint is what most of the apt list
# below is for: a fresh container with an empty data volume must be able to
# read captchas, rasterise scanned PDFs and *train its own captcha model* with
# no network access to anything except tntenders.gov.in.

FROM python:3.12-slim-bookworm

# ---------------------------------------------------------------------------
# System packages
#
# Each line here is a dependency of code in this repository, verified against
# the call site rather than guessed at. Nothing is installed "just in case":
# every package is a few tens of megabytes on an image that already carries a
# CPU build of PyTorch.
# ---------------------------------------------------------------------------
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && apt-get install -y --no-install-recommends \
      # The OCR engine. Two call sites, both load-bearing:
      #   * extract_text.py rasterises a scanned PDF with pdf2image and reads it
      #     with pytesseract — that is the only way the contents of a scanned
      #     tender document ever reach the full-text index, and "search inside
      #     the requirements" is the entire point of the archive.
      #   * captcha.py falls back to Tesseract when no trained CNN exists yet.
      #     It reads these captchas ~15% of the time (the portal's solid black
      #     noise squares fuse with the glyphs), which sounds useless and is
      #     not: config.toml sets captcha_attempts = 15, and each retry is an
      #     independent fresh captcha, so a fresh mirror still captures
      #     documents from day one — slowly — while it accumulates the verified
      #     labels a trained model needs.
      # tesseract-ocr pulls tesseract-ocr-eng and tesseract-ocr-osd, which is
      # the whole language requirement: every document on this portal is
      # English or numeric.
      tesseract-ocr \
      # pdftoppm, which pdf2image shells out to. Without it, extraction of a
      # scanned PDF fails at the rasterise step and the document stays
      # invisible to search forever — the text layer path (pdfplumber, pure
      # Python) would still work, but a scan has no text layer, which is
      # precisely why it needs OCR.
      poppler-utils \
      # DejaVu Sans. This is the single most important line in the file and the
      # one most likely to be dropped by someone trimming the image.
      #
      # captcha_synth.py reconstructs the portal's own captcha generator, and
      # the reconstruction is a font *identification*, not a resemblance:
      # rendering all 49 observed characters and scoring IoU against per-class
      # median glyphs from real captchas puts DejaVu Sans 38px at 0.81 against
      # 0.70 for the runner-up, with well-sampled classes matching 0.95-0.99 at
      # identical bounding-box dimensions. Without this font, captcha_synth
      # raises "DejaVu Sans not available", the synthetic corpus cannot be
      # built, and the CNN — which supplies essentially all of the training
      # signal, since the portal only issues one captcha per scrape session —
      # can never be trained. A fresh container would be permanently stuck on
      # the ~15% Tesseract fallback. That is a mirror that is dead on arrival,
      # so the entrypoint verifies this at every boot rather than trusting it.
      #
      # fonts-dejavu-core also supplies DejaVuSans-Bold.ttf, which
      # web/sharecard.py wants first for the OpenGraph share images; its
      # fallback list would otherwise land on PIL's bitmap default and render
      # unreadable cards.
      fonts-dejavu-core \
      # scripts/backup.sh — the documented backup path in docs/SELF-HOSTING.md
      # — is `sqlite3 .backup` plus rsync. `cp` of a live WAL database races
      # the scraper and yields a file whose WAL and main file disagree, so the
      # sqlite3 CLI is not a convenience here, it is the difference between a
      # backup and a corrupt copy of an irreplaceable archive.
      sqlite3 rsync \
      # HTTPS to the portal and to the push services.
      ca-certificates \
      # The two daily jobs fire at wall-clock times (04:30 and 03:15). Without
      # tzdata the container is UTC-only and "04:30" silently means something
      # five and a half hours away from the quiet hour it was chosen to be.
      tzdata \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Python dependencies
# ---------------------------------------------------------------------------

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# PyTorch, CPU build, from PyTorch's own index — installed *before* the project
# so that the project's `torch>=2.2` is already satisfied and pip never reaches
# for the default PyPI wheel.
#
# That default wheel is a CUDA build: it depends on nvidia-cublas, nvidia-cudnn,
# nvidia-cusparse and a dozen siblings, roughly 2 GB of accelerator libraries
# that cannot execute here at all. Nothing in this project uses a GPU — the
# captcha CNN is small enough that captcha_model.train() pins itself to 4 CPU
# threads specifically so it yields to the live scraper sharing the box — so
# those 2 GB would be pure image size, pure pull time, and pure attack surface.
#
# The cache mount keeps a rebuild from re-downloading ~200 MB of wheel while
# adding nothing to the final image.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url https://download.pytorch.org/whl/cpu torch

# The process supervisor. Installed from PyPI rather than apt because apt's
# `supervisor` package drags in Debian's system python3 alongside the 3.12
# already here — two interpreters, one of which nothing uses. supervisor is
# pure Python and runs happily on 3.12.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install "supervisor>=4.2"

WORKDIR /app
COPY . /app

# Editable install, and not for developer convenience.
#
# config.py resolves the project root as `Path(__file__).resolve().parents[2]`
# and resolves every relative path in config.toml's [paths] against it. Under a
# normal (copied) install that file lands at
# /usr/local/lib/python3.12/site-packages/tenders/config.py, whose parents[2]
# is /usr/local/lib/python3.12 — so `db = "data/tenders.db"` would resolve to
# /usr/local/lib/python3.12/data/tenders.db, inside the image, outside the
# volume, and silently discarded on every container restart. An editable
# install keeps the package rooted at /app/src/tenders, which makes
# PROJECT_ROOT /app and the data directory /app/data, which is where the volume
# is mounted. The alternative would be rewriting config.toml's paths to
# absolutes, and config.toml is a committed file this image has no business
# editing.
#
# The [ml] extra is what makes `tenders-captcha-train` exist as more than an
# error message; torch is already installed above, so this resolves it as
# satisfied and downloads nothing.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -e ".[ml]"

# ---------------------------------------------------------------------------
# The user everything runs as
#
# A stable uid/gid, because the archive outlives the container and file
# ownership on a bind mount is a property of the *host*, not of the image. If
# the uid changed between image versions, every rebuild would silently lose
# write access to 69 GB of captured documents.
#
# 1001 is the default because it is the uid this deployment's data directory is
# already owned by (healthcheck.py's Nice= comment documents the same uid from
# the other direction: RLIMIT_NICE is 0 for it, which is why the watchdog asks
# for Nice=0 rather than a negative value). Self-hosters whose host uid differs
# override it at build time:
#
#     docker compose build --build-arg TENDERS_UID=$(id -u) \
#                          --build-arg TENDERS_GID=$(id -g)
#
# This only matters for bind mounts. With the named volume that compose uses by
# default, Docker seeds an empty volume from the image directory *including its
# ownership*, so /app/data below is what makes a first run work with no chown
# at all.
# ---------------------------------------------------------------------------
ARG TENDERS_UID=1001
ARG TENDERS_GID=1001
RUN groupadd -g "${TENDERS_GID}" tenders \
 && useradd -u "${TENDERS_UID}" -g "${TENDERS_GID}" -m -s /bin/bash tenders \
 && mkdir -p /app/data \
 && chown "${TENDERS_UID}:${TENDERS_GID}" /app/data \
 && install -d -o "${TENDERS_UID}" -g "${TENDERS_GID}" -m 0755 /var/run/tenders

# The entrypoint is a multi-call script: what it does depends on the name it is
# invoked under and its first argument. The `systemctl` link is the important
# one and is explained at length in docker-entrypoint.sh — the short version is
# that scripts/healthcheck.py heals a wedged frontend by shelling out to
# `systemctl --user restart tenders-web.service`, that file is not ours to
# edit, and it exposes no environment override for the restart command (only
# for the probe URL and the state path). A shim on PATH translates that one
# call into the supervisor's equivalent, and the watchdog keeps working
# unmodified.
RUN cp /app/deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh \
 && chmod 0755 /usr/local/bin/docker-entrypoint.sh \
 && ln -sf /usr/local/bin/docker-entrypoint.sh /usr/local/bin/systemctl

ENV TENDERS_WEB_PORT=8013 \
    TENDERS_WEB_HOST=127.0.0.1 \
    SUPERVISOR_CONF=/app/deploy/supervisord.conf

# Deliberately NOT set here: OMP_THREAD_LIMIT=1.
#
# It looks like an obvious image-wide default — extract_text.py sets exactly
# that before forking its four OCR workers, so that four workers do not each
# spawn a thread pool on a box that is also serving a website. Hoisting it into
# the image was measured and reverted, because it does not only affect
# extraction: it is an OpenMP-wide ceiling, and PyTorch links OpenMP. Measured
# in this image, 20 multiplications of a 2000x2000 matrix take 4.51s with
# OMP_THREAD_LIMIT=1 and 1.69s without it — a 2.7x slowdown applied to the
# nightly captcha retrain, which asks for four threads by name
# (captcha_model.train sets torch.set_num_threads(4)) and is the one job whose
# whole purpose is to keep the mirror able to download documents at all.
#
# extract_text.py setting it for itself, in the process that wants it, is the
# correct scope. Note it uses os.environ.setdefault, so an inherited value
# would have won — which is exactly how a well-meant image default silently
# becomes a policy nobody chose.

USER tenders

EXPOSE 8013

# The container-level health check.
#
# It probes /healthz through scripts/healthcheck.py's *own* validation — the
# same wall-clock deadline, the same 16-byte floor, the same requirement that
# the body parse as JSON with at least one field — because a 200 status line is
# not evidence of health here. On 2026-08-15 the frontend accepted TCP
# connections and returned HTTP 200 while never writing a response body; a
# status-only check called that healthy for the entire outage.
#
# What it deliberately does *not* do is call healthcheck.py's main(), because
# main() restarts the web process. That job belongs to the in-container
# watchdog, which is the thing that knows about consecutive failures and the
# 3-restarts-per-hour storm guard. Two independent healers racing each other
# would restart a merely-busy site twice as often and defeat both guards, so
# this one only ever reports.
HEALTHCHECK --interval=60s --timeout=15s --start-period=120s --retries=3 \
  CMD ["docker-entrypoint.sh", "probe"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["supervisord"]
