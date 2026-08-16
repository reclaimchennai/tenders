#!/bin/sh
# TN Tenders Mirror — container entrypoint, and the two other things systemd
# used to do for us.
#
# On the host this project is seven systemd units: three long-running services,
# three timers and a oneshot. A container has an init, and that is all. This
# script supplies the parts of systemd that the supervisor does not:
#
#   1. First-boot preparation of the data volume (the tree, its ownership, and
#      a loud failure when the volume is not writable — see prepare_data).
#   2. Timers. `every` and `daily` reproduce OnUnitActiveSec= and OnCalendar=
#      closely enough that the reasoning in deploy/*.timer still applies, and
#      the comments there are worth reading before changing a number here.
#   3. `systemctl`. scripts/healthcheck.py heals a wedged frontend by running
#      `systemctl --user restart tenders-web.service`. That file is not ours to
#      edit — and, checked line by line, it exposes environment overrides for
#      the probe URL (TENDERS_HEALTHCHECK_URL, TENDERS_WEB_HOST,
#      TENDERS_WEB_PORT, TENDERS_HEALTHCHECK_PATH) and for the storm-guard
#      ledger (TENDERS_HEALTHCHECK_STATE), but *none at all* for the restart
#      command: restart_unit() has the argv hard-coded. So the container
#      supplies a `systemctl` on PATH that speaks supervisorctl. The watchdog
#      runs unmodified, keeps its exit codes, and keeps its timing budget
#      (RESTART_TIMEOUT_S=180 against a stop that is bounded at 30s here).
#
# It is one file rather than four because a container image should not sprout a
# private bin/ directory of half-scripts; which behaviour runs is decided by
# argv[0] and the first argument, busybox-style.

set -eu

APP_ROOT="${APP_ROOT:-/app}"
DATA_DIR="${DATA_DIR:-$APP_ROOT/data}"
STATE_DIR="$DATA_DIR/state"
SUPERVISOR_CONF="${SUPERVISOR_CONF:-$APP_ROOT/deploy/supervisord.conf}"

# Local time, not UTC, and formatted to match config.setup_logging's
# "%H:%M:%S LEVEL name | message" exactly. Everything in this container writes
# to one stdout stream, and two clocks in one log is how a five-and-a-half hour
# offset gets read as a five-and-a-half hour stall.
log() { printf '%s INFO    entrypoint | %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
# Same stream, higher level, so `docker logs … | grep -E 'ERROR|CRITICAL'`
# finds the container's own problems alongside the application's.
logerr() { printf '%s ERROR   entrypoint | %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
die() { logerr "FATAL: $*"; exit 1; }

# ---------------------------------------------------------------------------
# systemctl shim
#
# Only the verbs healthcheck.py actually uses are implemented, and an unknown
# one is a hard error rather than a silent success. A shim that pretended to
# restart the site would produce exactly the outcome the watchdog exists to
# prevent: an incident that reports itself as handled.
# ---------------------------------------------------------------------------

supervisorctl_() { supervisorctl -c "$SUPERVISOR_CONF" "$@"; }

unit_to_program() {
    # The mapping is one-way and explicit. Deriving the program name by
    # stripping "tenders-" and ".service" would quietly invent a target for any
    # typo, and `supervisorctl restart <nonexistent>` is not an error loud
    # enough to notice.
    case "$1" in
        tenders-web.service|tenders-web)                 echo web ;;
        tenders-scraper.service|tenders-scraper)         echo scraper ;;
        tenders-extract.service|tenders-extract)         echo extract ;;
        tenders-award-sweep.service|tenders-award-sweep) echo award-sweep ;;
        *) return 1 ;;
    esac
}

systemctl_shim() {
    verb=""
    unit=""
    for arg in "$@"; do
        case "$arg" in
            --user|--system|--no-block|-q|--quiet) ;;
            restart|start|stop|status|is-active) [ -n "$verb" ] || verb="$arg" ;;
            *) [ -n "$unit" ] || unit="$arg" ;;
        esac
    done
    [ -n "$verb" ] || die "systemctl shim: no verb in: $*"
    [ -n "$unit" ] || die "systemctl shim: no unit in: $*"

    program="$(unit_to_program "$unit")" || {
        # Non-zero and a message on stderr: healthcheck.py reports
        # `proc.stderr` verbatim in its EMERGENCY line, so this text is what a
        # human reads at 3am.
        echo "systemctl shim: no container program replicates $unit" >&2
        exit 1
    }

    case "$verb" in
        restart|start|stop)
            supervisorctl_ "$verb" "$program" || {
                echo "supervisorctl $verb $program failed" >&2
                exit 1
            }
            # supervisorctl exits 0 for outcomes that are not successes —
            # notably a program that starts and immediately dies reports
            # "ERROR (abnormal termination)" on stdout and still returns 0. The
            # watchdog's whole contract is that a failed restart must be
            # distinguishable from a successful one (EXIT_RESTART_FAILED vs
            # EXIT_RESTARTED_*), so the state is confirmed rather than assumed.
            if [ "$verb" != "stop" ]; then
                state="$(supervisorctl_ status "$program" 2>&1 || true)"
                case "$state" in
                    *RUNNING*) : ;;
                    *) echo "$program did not reach RUNNING: $state" >&2; exit 1 ;;
                esac
            fi
            ;;
        status)    supervisorctl_ status "$program" ;;
        is-active) supervisorctl_ status "$program" | grep -q RUNNING ;;
    esac
}

# ---------------------------------------------------------------------------
# Timers
# ---------------------------------------------------------------------------

# A sleep that a signal can interrupt. `sleep 300` in a shell script blocks the
# trap until it finishes, so a `docker compose down` would wait out the full
# interval on every timer before the container stopped. Backgrounding it and
# waiting means SIGTERM is handled at once, which is the difference between a
# 2-second shutdown and a 10-second SIGKILL.
interruptible_sleep() {
    sleep "$1" &
    wait $! 2>/dev/null || true
}

run_job() {
    # label, then the command.
    label="$1"; shift
    log "$label: starting"
    started="$(date +%s)"
    rc=0
    "$@" || rc=$?
    elapsed=$(( $(date +%s) - started ))
    if [ "$rc" -eq 0 ]; then
        log "$label: ok (${elapsed}s)"
    else
        # A non-zero exit is *expected* from two of these jobs and must not stop
        # the loop. healthcheck.py exits 1-4 whenever it had to act (that is its
        # reporting channel — it is how the failed-units list names an incident
        # without anyone grepping a journal), and db_maint.py exits 1 on a
        # quick_check that is not "ok". Both mean "read the log now", neither
        # means "stop checking".
        logerr "$label: exit $rc after ${elapsed}s — see the log above"
    fi
    # Stamped whether it succeeded or not, and that is deliberate: the stamp
    # answers "did this job get its turn today", not "did it like what it
    # found". db_maint.py exits 1 on a database whose quick_check is not "ok",
    # which is a condition that will still be true in two minutes — stamping
    # only on success would turn a corrupt database into a job that re-runs
    # every two minutes forever, competing for the disk with whatever a human
    # is doing to rescue it.
    # The redirection itself is inside the silenced group: a missing state
    # directory must not put a shell error in the log of a job that worked.
    { date -u '+%Y-%m-%dT%H:%M:%SZ' > "$STATE_DIR/$label.stamp"; } 2>/dev/null || true
    return "$rc"
}

# oneshot <retry_delay_s> <max_attempts> <label> -- <cmd...>
#
# `Type=simple` + `Restart=on-failure` + `RestartSec=` + `StartLimitBurst=`,
# implemented here rather than left to the supervisor, because supervisord
# cannot express that combination and the naive mapping is actively wrong.
#
# The job this exists for is the award sweep, which *finishes*: ~30 hours of
# work and then a clean exit 0, and on a fresh or already-swept archive it
# reaches that clean exit in under a second. supervisord decides whether an
# exit was "expected" only after a program has stayed up for `startsecs`, so a
# sweep that completes immediately is recorded as a failed *start* and retried
# — ten times, in three seconds, before landing in FATAL. Measured, not
# theorised: `exited: award-sweep (exit status 0; not expected)` four times in
# nine seconds on an empty archive.
#
# Setting startsecs=0 fixes that and breaks something worse: it also removes
# the only bound supervisord has on restarts, turning a sweep that crashes on
# its first portal request into a loop that re-issues that request as fast as
# it can crash. This job talks to a live government portal. An unpaced retry
# loop is exactly the thing this project must never do.
#
# So the policy lives here, where all three parts of it can be expressed at
# once: a clean exit is final, a failure waits RestartSec seconds, and after
# StartLimitBurst attempts it stops and stays stopped so that a genuinely
# broken sweep looks broken instead of looking busy.
oneshot() {
    delay="$1"; max="$2"; label="$3"; shift 3
    if [ "${1:-}" = "--" ]; then shift; fi
    attempt=1
    while :; do
        rc=0
        run_job "$label" "$@" || rc=$?
        if [ "$rc" -eq 0 ]; then
            log "$label: completed. Not restarting — this job ends, and a"
            log "$label: finished sweep that keeps restarting looks like progress."
            return 0
        fi
        if [ "$attempt" -ge "$max" ]; then
            logerr "$label: EMERGENCY: failed $attempt times (last exit $rc). Giving up."
            logerr "$label: This is deliberate. Restarting a job that has failed $max"
            logerr "$label: times in a row is not fixing it, and it costs requests"
            logerr "$label: against a public portal every time. A human needs to look."
            return "$rc"
        fi
        logerr "$label: exit $rc — attempt $attempt of $max; retrying in ${delay}s"
        attempt=$((attempt + 1))
        interruptible_sleep "$delay"
    done
}

# every <interval_s> <initial_delay_s> <label> -- <cmd...>
#
# OnBootSec= + OnUnitActiveSec=, with Persistent=false, which is what both
# sub-minute timers here set. deploy/tenders-healthcheck.timer explains why
# catch-up would be wrong for these: they ask a present-tense question ("is the
# site serving *now*"), and a run fired to make up for a window when the
# container was down would only probe a service that has not finished starting.
every() {
    interval="$1"; delay="$2"; label="$3"; shift 3
    if [ "${1:-}" = "--" ]; then shift; fi
    log "$label: timer every ${interval}s, first run in ${delay}s"
    interruptible_sleep "$delay"
    while :; do
        # `|| true` because a non-zero exit is the *reporting channel* for two
        # of these jobs, not a reason to stop timing them: healthcheck.py exits
        # 1-4 whenever it had to act, db_maint.py exits 1 on a quick_check that
        # is not "ok". Both mean "read the log", neither means "stop checking".
        run_job "$label" "$@" || true
        interruptible_sleep "$interval"
    done
}

# daily <HH:MM> <max_jitter_s> <label> -- <cmd...>
#
# OnCalendar=*-*-* HH:MM:00 with RandomizedDelaySec= and Persistent=true. The
# jitter is not decoration: the two daily jobs are a ~30-minute 4-thread CNN
# retrain and a full page scan of the database, and deploy/tenders-dbmaint.timer
# spends a paragraph on keeping them off each other's disk.
#
# Persistent=true is reproduced with a stamp file under data/state. A container
# that was down at 04:30 genuinely did not examine the WAL, and the WAL is the
# thing that reached 44 GB against a 1.9 GB database and took the site down —
# that check is still worth doing late. The catch-up is deliberately not
# immediate: 120 seconds of settle time lets the frontend finish opening the
# database first, so the maintenance job is not competing with start-up.
daily() {
    at="$1"; jitter="$2"; label="$3"; shift 3
    if [ "${1:-}" = "--" ]; then shift; fi
    stamp="$STATE_DIR/$label.stamp"

    if [ ! -f "$stamp" ] || [ -z "$(find "$stamp" -mtime -1 2>/dev/null)" ]; then
        log "$label: has not run in the last 24h (Persistent=true catch-up); running in 120s"
        interruptible_sleep 120
        run_job "$label" "$@" || true
    fi

    while :; do
        now="$(date +%s)"
        target="$(date -d "today $at" +%s 2>/dev/null || echo "")"
        [ -n "$target" ] || die "$label: cannot parse schedule '$at' (want HH:MM)"
        [ "$target" -le "$now" ] && target="$(date -d "tomorrow $at" +%s)"
        wait_s=$(( target - now ))
        if [ "$jitter" -gt 0 ]; then
            # $$ is stable per container, so this is a fixed offset per boot
            # rather than a fresh draw each day. Spreading the two jobs apart is
            # the point; re-rolling it nightly is not.
            wait_s=$(( wait_s + ($$ % jitter) ))
        fi
        log "$label: next run at $at (+jitter), sleeping ${wait_s}s"
        interruptible_sleep "$wait_s"
        run_job "$label" "$@" || true
        # A second's grace so a job that finished in under a second cannot
        # compute the same target time again and fire twice.
        interruptible_sleep 2
    done
}

# ---------------------------------------------------------------------------
# The container HEALTHCHECK probe
#
# Reuses healthcheck.py's validate_response and probe_once rather than curling
# the URL, so "healthy" means exactly what the watchdog means by it: a complete
# HTTP 200 whose body is at least 16 bytes and parses as JSON with at least one
# field, delivered inside a hard wall-clock deadline. A curl -f would have
# reported the 2026-08-15 wedge as healthy for its entire duration.
#
# It reports and does not heal. See the HEALTHCHECK comment in the Dockerfile.
# ---------------------------------------------------------------------------
probe() {
    # scripts/ is not part of the installed package (the systemd units invoke
    # these files by path), so it has to be put on the path to import from.
    PYTHONPATH="$APP_ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}"
    export PYTHONPATH
    exec python -c '
import sys
import healthcheck as hc
url = hc.probe_url()
result = hc.probe_once(url)
print(f"{url} -> {result}")
sys.exit(0 if result.ok else 1)
'
}

# ---------------------------------------------------------------------------
# First-boot preparation
# ---------------------------------------------------------------------------

prepare_data() {
    # Running as root only happens when the operator explicitly asked for it
    # (`user: root`, or a --user override). The image's default is the
    # unprivileged tenders user, so this branch is the bind-mount escape hatch,
    # not the normal path.
    if [ "$(id -u)" = "0" ]; then
        log "running as root: adjusting $DATA_DIR ownership, then dropping privileges"
        # Non-recursive, and that is a deliberate refusal.
        #
        # `chown -R` over this volume would walk 39,000 html directories and
        # 69 GB of captured documents — minutes of pure metadata churn on every
        # single container start, against files that exist nowhere else because
        # the portal has already deleted them. The top directory is enough to
        # let the application create what it needs; if files *inside* are owned
        # by a uid that cannot write them, that is a real migration decision
        # for a human to make with a command they typed themselves, and
        # docs/SELF-HOSTING.md gives them that command.
        chown "$(id -u tenders):$(id -g tenders)" "$DATA_DIR" || true
        exec setpriv --reuid tenders --regid tenders --init-groups "$0" "$@"
    fi

    [ -d "$DATA_DIR" ] || die "$DATA_DIR does not exist"

    # The single most common self-hosting failure: a bind-mounted host
    # directory owned by a different uid. Detect it here and say precisely what
    # to type, because the alternative is a stack trace from sqlite3 five
    # seconds later that names a permission problem without naming the fix.
    if ! touch "$DATA_DIR/.write-test" 2>/dev/null; then
        owner="$(stat -c '%u:%g' "$DATA_DIR" 2>/dev/null || echo '?')"
        die "$DATA_DIR is not writable by uid $(id -u) (it is owned by $owner).
    If this is a bind mount, on the HOST run:
        sudo chown -R $(id -u):$(id -g) /path/to/your/data
    Or rebuild the image for your own uid:
        docker compose build --build-arg TENDERS_UID=\$(id -u) --build-arg TENDERS_GID=\$(id -g)
    See docs/SELF-HOSTING.md, 'Permissions'."
    fi
    rm -f "$DATA_DIR/.write-test"

    # The project's own directory maker, rather than a second list of mkdirs
    # that would drift from it the first time a path is added to config.toml.
    python -c 'from tenders.config import load_config; load_config().ensure_dirs()'

    # ensure_dirs() covers the database directory, docs, html and captcha. Two
    # more are needed here:
    #   secrets/ — push.write_key() creates it 0700 when a key is generated,
    #     but creating it up front means the tree is complete and predictable
    #     for a restore (drop a vapid_private.pem in and restart), and 0700 is
    #     set here for the same reason it is set there: that file is the
    #     credential that can push to every subscriber's device.
    #   state/ — this script's timer stamps. It lives on the volume so that
    #     Persistent=true survives a container rebuild, exactly as
    #     healthcheck.py puts its restart ledger next to the database so the
    #     storm guard survives a reboot.
    mkdir -p "$DATA_DIR/secrets" && chmod 0700 "$DATA_DIR/secrets"
    mkdir -p "$STATE_DIR"
}

preflight() {
    log "TN Tenders Mirror — data=$DATA_DIR  web port=$TENDERS_WEB_PORT  TZ=${TZ:-UTC}"

    # The no-external-dependency check, run at every boot and not merely at
    # build time. An image is a moving target — a base-image bump, a
    # someone-trimmed-the-apt-list commit — and the failure mode is silent: the
    # mirror keeps working, downloads keep succeeding at the Tesseract fallback
    # rate, and only weeks later does anyone notice the CNN was never trainable.
    # Six lines of Python at boot converts that into a line in `docker logs`.
    python - <<'PY' || log "WARNING: synthetic captcha generation is NOT available (see above)"
import random
import sys
from tenders import captcha_synth
if not captcha_synth.available():
    print("  captcha_synth: DejaVu Sans NOT FOUND. The captcha CNN cannot be",
          "trained in this image, and document downloads will fall back to",
          "Tesseract (~15%). Install fonts-dejavu-core.", file=sys.stderr)
    sys.exit(1)
img, text = captcha_synth.make(random.Random(0))
print(f"  captcha_synth: ok — DejaVu Sans found, sample {img.size} {text!r}",
      file=sys.stderr)
PY

    if python -c 'import pytesseract, sys; sys.exit(0 if pytesseract.get_tesseract_version() else 1)' >/dev/null 2>&1; then
        log "  tesseract: $(tesseract --version 2>&1 | head -1)"
    else
        # Not fatal. config.toml [ocr].enabled exists precisely so a mirror
        # without Tesseract still extracts PDF text layers; it just cannot read
        # scans, and it loses the captcha fallback that carries a fresh mirror
        # until its CNN is trained.
        log "  WARNING: tesseract is not usable — scanned PDFs will not be indexed"
    fi
    command -v pdftoppm >/dev/null 2>&1 \
        || log "  WARNING: pdftoppm is missing — scanned PDFs cannot be rasterised for OCR"

    # Absent by design on a fresh mirror: a container that has never scraped has
    # no labelled captchas and therefore no model. Downloads still work through
    # the Tesseract fallback while verified labels accumulate. Said out loud so
    # a new self-hoster reads it as a stage, not a fault.
    if [ -f "$DATA_DIR/captcha/model.pt" ]; then
        log "  captcha model: present ($DATA_DIR/captcha/model.pt)"
    else
        log "  captcha model: none yet — downloads use the Tesseract fallback."
        log "                 See docs/SELF-HOSTING.md, 'Getting a captcha model'."
    fi

    if [ -f "$DATA_DIR/secrets/vapid_private.pem" ]; then
        log "  web push: VAPID key present"
    else
        log "  web push: no VAPID key — notifications are off, the archive serves normally"
    fi
}

# Defaults for everything supervisord.conf interpolates. supervisord fails to
# start on an undefined %(ENV_x)s rather than defaulting it, so every switch
# used there must be given a value here.
export_defaults() {
    : "${TENDERS_WEB_PORT:=8013}"
    : "${TENDERS_WEB_HOST:=127.0.0.1}"

    # Per-process switches. Their first purpose is ordinary operation — a
    # mirror can serve its archive read-only with the scraper off, which is
    # what you want while a captcha model trains — and their second is that
    # they make it possible to smoke-test this image without sending a single
    # request to a live government portal.
    : "${TENDERS_ENABLE_WEB:=true}"
    : "${TENDERS_ENABLE_SCRAPER:=true}"
    : "${TENDERS_ENABLE_EXTRACT:=true}"
    : "${TENDERS_ENABLE_AWARD_SWEEP:=true}"
    : "${TENDERS_ENABLE_WATCH:=true}"
    : "${TENDERS_ENABLE_HEALTHCHECK:=true}"
    : "${TENDERS_ENABLE_DBMAINT:=true}"
    : "${TENDERS_ENABLE_CAPTCHA_TRAIN:=true}"

    # Schedules, matching deploy/*.timer. Read those files before changing one.
    : "${TENDERS_DBMAINT_AT:=04:30}"
    : "${TENDERS_CAPTCHA_TRAIN_AT:=03:15}"
    : "${TENDERS_HEALTHCHECK_INTERVAL:=120}"
    : "${TENDERS_WATCH_INTERVAL:=300}"

    # tenders-run's cycle pause and cancelled-sweep cadence, from
    # deploy/tenders-scraper.service. Exposed because they are the two numbers
    # that decide how much the mirror asks of the portal, and lowering them is
    # the mistake this project most wants a self-hoster not to make by
    # accident — so they are visible, documented and defaulted to the live
    # deployment's values rather than buried in a command line.
    : "${TENDERS_CYCLE_PAUSE:=900}"
    : "${TENDERS_CANCELLED_EVERY:=6}"

    export TENDERS_WEB_PORT TENDERS_WEB_HOST \
           TENDERS_ENABLE_WEB TENDERS_ENABLE_SCRAPER TENDERS_ENABLE_EXTRACT \
           TENDERS_ENABLE_AWARD_SWEEP TENDERS_ENABLE_WATCH \
           TENDERS_ENABLE_HEALTHCHECK TENDERS_ENABLE_DBMAINT \
           TENDERS_ENABLE_CAPTCHA_TRAIN \
           TENDERS_DBMAINT_AT TENDERS_CAPTCHA_TRAIN_AT \
           TENDERS_HEALTHCHECK_INTERVAL TENDERS_WATCH_INTERVAL \
           TENDERS_CYCLE_PAUSE TENDERS_CANCELLED_EVERY
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

case "$(basename "$0")" in
    systemctl) systemctl_shim "$@"; exit $? ;;
esac

mode="${1:-supervisord}"
case "$mode" in
    every|daily|oneshot) shift; export_defaults; "$mode" "$@" ;;
    probe)       probe ;;
    supervisord)
        export_defaults
        prepare_data "$@"
        preflight
        cd "$APP_ROOT"
        exec supervisord -c "$SUPERVISOR_CONF"
        ;;
    # Anything else is run as-is, so `docker compose run --rm tenders
    # tenders-stats` and `… tenders-captcha-train` work without fighting the
    # entrypoint. The data preparation still runs first: a one-off command
    # against an uninitialised volume should not be the thing that discovers
    # the volume is unwritable.
    *)
        export_defaults
        prepare_data "$@"
        cd "$APP_ROOT"
        exec "$@"
        ;;
esac
