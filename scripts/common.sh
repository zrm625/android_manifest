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

# The shared device lock, $BENCH_HOME/.device-lock (see qa/lib/riverqa/lock.py).
# With BENCH_LOCK_TOKEN set, reuse the lock only if the token matches the lock
# file and someone holds it; otherwise take the lock for this script (fd 8).
device_lock() {
  local f=$BENCH_HOME/.device-lock
  mkdir -p "$BENCH_HOME"
  if [[ -n "${BENCH_LOCK_TOKEN:-}" ]]; then
    [[ "$(tr -d '[:space:]' < "$f" 2>/dev/null)" == "$BENCH_LOCK_TOKEN" ]] \
      || { echo "error: BENCH_LOCK_TOKEN does not match the device lock; refusing to run" >&2; exit 1; }
    if flock -n "$f" true; then
      echo "error: BENCH_LOCK_TOKEN is set but nobody holds the device lock; refusing to run" >&2; exit 1
    fi
    return 0
  fi
  exec 8<>"$f"
  flock -n 8 || { echo "error: another device job holds $f" >&2; exit 1; }
  BENCH_LOCK_TOKEN=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
  : > "$f"
  printf '%s\n' "$BENCH_LOCK_TOKEN" > "$f"
  export BENCH_LOCK_TOKEN
}
