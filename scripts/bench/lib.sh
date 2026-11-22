# Shared helpers for build, bench and the other bench scripts. Source this
# file; do not run it.

source "$(dirname "$(realpath "${BASH_SOURCE[0]}")")/../common.sh"

BENCH_HOME=${BENCH_HOME:-/mnt/storage/lineageos-bench}
BASELINE_TREE=${BASELINE_TREE:-$HOME/git/lineageos-river-baseline}
BENCH_DIR=$(dirname "$(realpath "${BASH_SOURCE[0]}")")
CCACHE_HOME=${CCACHE_HOME:-$BENCH_HOME/ccache}
CCACHE_MAX_SIZE=${CCACHE_MAX_SIZE:-100G}

# Print the source tree for "baseline" or "stripped".
tree_for() {
  case "$1" in
    baseline) echo "$BASELINE_TREE" ;;
    stripped) echo "$TOP" ;;
    *) return 1 ;;
  esac
}

# One build at a time, whether a dev build or a benchmark. Two at once would
# overload the machine and make any timing meaningless.
acquire_build_lock() {
  mkdir -p "$BENCH_HOME"
  exec 9>"$BENCH_HOME/.lock"
  flock -n 9 || { echo "error: another build is running" >&2; exit 1; }
}

# Build the pinned image if needed and set IMAGE.
ensure_image() {
  IMAGE="lineageos-bench:$(sha256sum "$BENCH_DIR/Containerfile" | cut -c1-12)"
  if ! podman image exists "$IMAGE"; then
    echo "Building $IMAGE"
    podman build -t "$IMAGE" -f "$BENCH_DIR/Containerfile" "$BENCH_DIR"
  fi
}

# run_build TREE OUT TARGET JOBS CCACHE(0|1) LOG
# Runs `m TARGET` in the container with TREE read-only and OUT writable.
# Writes "<exit code> <seconds> <peak memory bytes>" to OUT/.bench-result
# and returns the exit code of m.
run_build() {
  local tree=$1 out=$2 target=$3 jobs=$4 use_ccache=$5 log=$6
  local ccache_args
  if [[ "$use_ccache" == 1 ]]; then
    mkdir -p "$CCACHE_HOME"
    ccache_args=(-v "$CCACHE_HOME:/ccache:rw" -e USE_CCACHE=1 -e CCACHE_EXEC=/usr/bin/ccache
                 -e CCACHE_DIR=/ccache -e CCACHE_BASEDIR=/src -e CCACHE_MAX_SIZE="$CCACHE_MAX_SIZE")
  else
    ccache_args=(-e USE_CCACHE=0 -e CCACHE_DISABLE=1)
  fi
  mkdir -p "$out"
  # The read-only tree needs an out/ mount point for the writable out mount.
  [[ -d "$tree/out" ]] || mkdir "$tree/out"
  rm -f "$out/.bench-result"
  set +e
  # HOME must be writable: Soong keeps a Go build cache in $HOME/.cache.
  podman run --rm --name "lineageos-build" --userns=keep-id \
    --tmpfs /home/build:exec -e HOME=/home/build -e USER=build \
    -v "$tree:/src:ro" -v "$out:/src/out:rw" -w /src \
    "${ccache_args[@]}" -e BUILD_TARGET="$target" -e BUILD_JOBS="$jobs" \
    "$IMAGE" bash -c '
      source build/envsetup.sh >/dev/null
      breakfast river >/dev/null || { echo "90 0 " > out/.bench-result; exit 90; }
      start=$(date +%s.%N)
      m "$BUILD_TARGET" -j"$BUILD_JOBS"
      rc=$?
      end=$(date +%s.%N)
      peak=$(cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo "")
      echo "$rc $(echo "$end - $start" | bc) $peak" > out/.bench-result
      [[ "${USE_CCACHE:-0}" == 1 ]] && ccache --show-stats --verbose 2>/dev/null | grep -E "Hits|Misses|Cache size" | head -6
      exit $rc
    ' 2>&1 | tee "$log"
  local rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}
