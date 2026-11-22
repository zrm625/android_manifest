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
