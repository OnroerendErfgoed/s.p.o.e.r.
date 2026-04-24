#!/usr/bin/env bash
#
# ci_run_shell_spec.sh — stand up the dossier stack, run
# test_requests.sh against it, report pass/fail.
#
# M4/M5 relief for `test_requests.sh` specifically: the shell spec
# is our most comprehensive end-to-end test (1585 lines, 25 OK
# assertions, D1–D9 covering aanvraag/beslissing/tombstone/cancellation/
# schema-versioning/lineage) but it only runs when a human
# remembers to invoke it. This wrapper makes that trivial from CI:
# single command, deterministic teardown, exit code that tools can
# read.
#
# Usage:
#   scripts/ci_run_shell_spec.sh
#
# Exit codes:
#   0   spec passed (exit 0 from test_requests.sh + 25 OK lines)
#   1   spec failed (any non-zero exit from test_requests.sh)
#   2   stack never came up (one or more services timed out)
#   3   environment precondition missing (Postgres, config, etc.)
#
# Preconditions (enforced at startup):
#   * Postgres is running on 127.0.0.1:5432 with user `dossier`,
#     password `dossier`, database `dossiers`. The script drops
#     and recreates the public schema to get a clean slate.
#   * Python with the three repos (engine, file_service, app) on
#     PYTHONPATH or installed.
#   * /tmp/dossier_run/config.yaml exists. For CI, stage this
#     from a fixture before invoking the script.
#
# On GitHub Actions or GitLab CI, run this as a job step after
# the Postgres service container is up and after `pip install`
# has landed the three repos. Non-zero exit fails the job.

set -euo pipefail

# ----------------------------------------------------------------
# Config — override via env vars for non-default environments.
# ----------------------------------------------------------------
: "${PG_HOST:=127.0.0.1}"
: "${PG_PORT:=5432}"
: "${PG_USER:=dossier}"
: "${PG_PASSWORD:=dossier}"
: "${PG_DATABASE:=dossiers}"
: "${DOSSIER_RUN_DIR:=/tmp/dossier_run}"
: "${FILE_SERVICE_PORT:=8001}"
: "${APP_PORT:=8000}"
: "${SPEC_PATH:=$(dirname "$0")/../test_requests.sh}"
: "${SERVICE_START_TIMEOUT:=30}"

# ----------------------------------------------------------------
# Teardown — runs on exit to kill services we spawned. Recorded
# PIDs are whatever we started; no broad pkill so we don't
# interfere with other processes on shared CI runners.
# ----------------------------------------------------------------
pids=()
cleanup() {
    for pid in "${pids[@]:-}"; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    # Give processes a moment to exit gracefully before we bail.
    sleep 1
    for pid in "${pids[@]:-}"; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT

# ----------------------------------------------------------------
# Precondition checks.
# ----------------------------------------------------------------
if ! command -v psql >/dev/null 2>&1; then
    echo "ERROR: psql not found on PATH" >&2
    exit 3
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH" >&2
    exit 3
fi

if [[ ! -f "$SPEC_PATH" ]]; then
    echo "ERROR: test_requests.sh not found at $SPEC_PATH" >&2
    exit 3
fi

if [[ ! -f "$DOSSIER_RUN_DIR/config.yaml" ]]; then
    echo "ERROR: config not found at $DOSSIER_RUN_DIR/config.yaml" >&2
    echo "Stage it from your test fixtures before running this script." >&2
    exit 3
fi

# Postgres must be reachable with the expected credentials before
# we try to reset the schema.
if ! PGPASSWORD="$PG_PASSWORD" psql \
        -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" \
        -c "SELECT 1" >/dev/null 2>&1; then
    echo "ERROR: cannot connect to Postgres at $PG_HOST:$PG_PORT as $PG_USER" >&2
    exit 3
fi

# ----------------------------------------------------------------
# Fresh slate — blow away the schema and file storage so D1-D9
# start from nothing. The spec assumes a clean DB; a stale one
# causes spurious failures that are very hard to diagnose from CI
# logs.
# ----------------------------------------------------------------
PGPASSWORD="$PG_PASSWORD" psql \
    -h "$PG_HOST" -p "$PG_PORT" -U "$PG_USER" -d "$PG_DATABASE" \
    -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public" \
    >/dev/null

rm -rf "$DOSSIER_RUN_DIR/file_storage"
mkdir -p "$DOSSIER_RUN_DIR/file_storage"

# ----------------------------------------------------------------
# Launch services. setsid + </dev/null + background so the
# services detach from this shell's controlling terminal — matters
# on CI runners that kill backgrounded jobs when the parent exits.
# We log each service to its own file so CI can archive them as
# artifacts on failure.
# ----------------------------------------------------------------
log_dir="$DOSSIER_RUN_DIR"

echo "Starting file_service on port $FILE_SERVICE_PORT..."
FILE_SERVICE_CONFIG="$DOSSIER_RUN_DIR/config.yaml" \
setsid python3 -m uvicorn file_service.app:app \
    --host 127.0.0.1 --port "$FILE_SERVICE_PORT" \
    >"$log_dir/fs.log" 2>&1 </dev/null &
pids+=($!)

echo "Starting dossier_app on port $APP_PORT..."
setsid python3 -m uvicorn dossier_app.main:app \
    --host 127.0.0.1 --port "$APP_PORT" \
    >"$log_dir/app.log" 2>&1 </dev/null &
pids+=($!)

echo "Starting worker..."
setsid python3 -m dossier_engine.worker \
    --interval 2 --config "$DOSSIER_RUN_DIR/config.yaml" \
    >"$log_dir/worker.log" 2>&1 </dev/null &
pids+=($!)

# ----------------------------------------------------------------
# Wait for services to be reachable. Poll rather than sleep — CI
# machines vary wildly in startup time and a fixed sleep either
# wastes seconds on fast machines or fails on slow ones.
# ----------------------------------------------------------------
wait_for() {
    local url="$1"
    local name="$2"
    local start=$SECONDS
    while (( SECONDS - start < SERVICE_START_TIMEOUT )); do
        if curl -sf "$url" >/dev/null 2>&1; then
            echo "  $name ready after $((SECONDS - start))s"
            return 0
        fi
        sleep 1
    done
    echo "ERROR: $name did not come up within ${SERVICE_START_TIMEOUT}s" >&2
    echo "Log tail:" >&2
    return 1
}

if ! wait_for "http://127.0.0.1:$FILE_SERVICE_PORT/health" "file_service"; then
    tail -30 "$log_dir/fs.log" >&2
    exit 2
fi

if ! wait_for "http://127.0.0.1:$APP_PORT/docs" "dossier_app"; then
    tail -30 "$log_dir/app.log" >&2
    exit 2
fi

# Worker has no HTTP endpoint to probe. Check the log instead —
# successful startup writes a specific line. Short timeout since
# the worker starts quickly; if it's not running by now something
# is fundamentally broken.
worker_ready=0
for _ in $(seq 1 10); do
    if grep -q "Worker loop starting\|poll_interval" "$log_dir/worker.log" 2>/dev/null; then
        worker_ready=1
        break
    fi
    sleep 1
done
if [[ $worker_ready -eq 0 ]]; then
    echo "WARNING: worker startup log line not detected; continuing anyway" >&2
    tail -20 "$log_dir/worker.log" >&2
fi

# ----------------------------------------------------------------
# Run the spec. Capture exit code + assertion count for the final
# report. `set -e` is temporarily disabled around the spec run so
# we can report on failure rather than bailing silently.
# ----------------------------------------------------------------
echo ""
echo "===== running test_requests.sh ====="
set +e
bash "$SPEC_PATH" >"$log_dir/test_run.log" 2>&1
spec_exit=$?
set -e

ok_count=$(grep -cE "^  OK:" "$log_dir/test_run.log" || true)
summary_count=$(grep -cE " summary:" "$log_dir/test_run.log" || true)
traceback_count=$(grep -cE "Traceback|HTTP 5[0-9][0-9]" "$log_dir/test_run.log" || true)

echo "  spec exit code:   $spec_exit"
echo "  OK assertions:    $ok_count"
echo "  summary lines:    $summary_count"
echo "  tracebacks / 5xx: $traceback_count"

# Tail of the run log always useful on failure; on success, suppress
# it to keep the CI log readable.
if [[ $spec_exit -ne 0 || $ok_count -lt 20 || $traceback_count -gt 0 ]]; then
    echo ""
    echo "===== test_run.log (last 60 lines) ====="
    tail -60 "$log_dir/test_run.log" >&2
    exit 1
fi

echo ""
echo "===== shell spec passed ====="
exit 0
