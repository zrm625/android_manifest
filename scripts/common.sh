# Shared helpers for the zrm625 fork scripts. Source this file; do not run it.

FORK_BRANCH=river-22.2
FORK_REMOTE=zrm625
FORK_GROUP=zrm625-fork

# The top of the repo tree: two levels above .repo/manifests.
TOP=$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/../../.." && pwd)

if [[ ! -d "$TOP/.repo" ]]; then
  echo "error: cannot find .repo above $TOP" >&2
  exit 1
fi

if [[ -n "${REPO:-}" ]]; then
  :
elif command -v repo >/dev/null 2>&1; then
  REPO=$(command -v repo)
else
  REPO="$TOP/.repo/repo/repo"
fi

BENCH_HOME=${BENCH_HOME:-/mnt/storage/lineageos-bench}

MANIFEST_DIR="$TOP/.repo/manifests"
# The scripts run from the manifest checkout they live in, which is not always
# $TOP/.repo/manifests (a worktree, for one).
SCRIPTS_DIR=$(dirname "$(realpath "${BASH_SOURCE[0]}")")
QA_LIB="$SCRIPTS_DIR/../qa/lib"

# H15: refuse while a live batch-runner child of another session exists
# (qa/lib/riverqa/children.py). Called after flock, before any token is
# written; the caller releases its lock on failure.
refuse_orphans() {
  python3 -I "$QA_LIB/riverqa/children.py"
}

# The runner's state file (qa/RUNNER.md, R4.1). Read under its lock, shared;
# fail closed if it exists and does not parse. Prints why and exits 1 while a
# batch is running or a tree lease is held.
refuse_while_batch() {
  python3 -I "$QA_LIB/riverqa/runnerstate.py" || exit 1
}

# One build at a time, whether a dev build, a benchmark or a batch: the build
# lock $BENCH_HOME/.lock (H1). It is opened without truncating, taken with
# flock -n on fd 9, then the orphan check runs, and only then a fresh token is
# written and exported as BENCH_BUILD_LOCK_TOKEN. With BENCH_BUILD_LOCK_TOKEN
# already set (a batch runner child), no fd is opened: the lock is reused only
# if the file holds that token and someone holds the lock.
acquire_build_lock() {
  local f=$BENCH_HOME/.lock
  mkdir -p "$BENCH_HOME"
  if [[ -n "${BENCH_BUILD_LOCK_TOKEN:-}" ]]; then
    [[ "$(tr -d '[:space:]' < "$f" 2>/dev/null)" == "$BENCH_BUILD_LOCK_TOKEN" ]] \
      || { echo "error: BENCH_BUILD_LOCK_TOKEN does not match the build lock; refusing to run" >&2; exit 1; }
    if flock -n "$f" true; then
      echo "error: BENCH_BUILD_LOCK_TOKEN is set but nobody holds the build lock; refusing to run" >&2; exit 1
    fi
    refuse_orphans || exit 1
    return 0
  fi
  exec 9<>"$f"
  flock -n 9 || { echo "error: a build or batch is running (it holds $f)" >&2; exit 1; }
  if ! refuse_orphans; then flock -u 9; exec 9>&-; exit 1; fi
  BENCH_BUILD_LOCK_TOKEN=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
  printf '%s\n' "$BENCH_BUILD_LOCK_TOKEN" > "$f"
  export BENCH_BUILD_LOCK_TOKEN
}

# H2: true if a build holds $BENCH_HOME/.lock. A build lock held under our own
# BENCH_BUILD_LOCK_TOKEN (the batch runner's) counts as no build.
build_running() {
  local f=$BENCH_HOME/.lock
  [[ -e "$f" ]] || return 1
  if [[ -n "${BENCH_BUILD_LOCK_TOKEN:-}" && "$(tr -d '[:space:]' < "$f" 2>/dev/null)" == "$BENCH_BUILD_LOCK_TOKEN" ]] \
     && ! flock -n -s "$f" true; then
    return 1
  fi
  ! flock -n -s "$f" true
}

# The shared device lock, $BENCH_HOME/.device-lock (see qa/lib/riverqa/lock.py).
# With BENCH_LOCK_TOKEN set, reuse the lock only if the token matches the lock
# file and someone holds it; otherwise take the lock for this script (fd 8).
# Either way no device job runs while a build holds the build lock (H2), or
# while a batch-runner child of another session lives (H15, after flock).
device_lock() {
  local f=$BENCH_HOME/.device-lock
  mkdir -p "$BENCH_HOME"
  if build_running; then
    echo "error: a build holds $BENCH_HOME/.lock; device jobs wait until it finishes" >&2; exit 1
  fi
  if [[ -n "${BENCH_LOCK_TOKEN:-}" ]]; then
    [[ "$(tr -d '[:space:]' < "$f" 2>/dev/null)" == "$BENCH_LOCK_TOKEN" ]] \
      || { echo "error: BENCH_LOCK_TOKEN does not match the device lock; refusing to run" >&2; exit 1; }
    if flock -n "$f" true; then
      echo "error: BENCH_LOCK_TOKEN is set but nobody holds the device lock; refusing to run" >&2; exit 1
    fi
    refuse_orphans || exit 1
    return 0
  fi
  exec 8<>"$f"
  flock -n 8 || { echo "error: another device job holds $f" >&2; exit 1; }
  if ! refuse_orphans; then flock -u 8; exec 8>&-; exit 1; fi
  BENCH_LOCK_TOKEN=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
  printf '%s\n' "$BENCH_LOCK_TOKEN" > "$f"
  export BENCH_LOCK_TOKEN
}
