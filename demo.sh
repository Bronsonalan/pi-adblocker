#!/bin/sh
# Fetch the latest piadblock checkout and run the local status-page demo.
# No git, no sudo, no DNS. Safe to re-run after Ctrl-C to pick up new pushes.
#
# Main (after merge):
#   curl -fsSL https://raw.githubusercontent.com/Bronsonalan/pi-adblocker/main/demo.sh | bash
#
# This PR's branch (before merge), pass the branch as the first argument:
#   curl -fsSL https://raw.githubusercontent.com/Bronsonalan/pi-adblocker/BRANCH/demo.sh | bash -s -- BRANCH
#
# Or set PIADBLOCK_DEMO_BRANCH. Extra args after -- go to `python3 -m piadblock demo`
# (for example `--port 8081`).

set -eu

branch="${PIADBLOCK_DEMO_BRANCH:-main}"
demo_args=""
if [ "$#" -gt 0 ]; then
  case "$1" in
    -*)
      demo_args="$*"
      ;;
    *)
      branch="$1"
      shift
      demo_args="$*"
      ;;
  esac
fi

dest="${PIADBLOCK_DEMO_DIR:-$HOME/pi-adblocker-demo}"
# Encode only characters that break a path segment; keep / so branch names like
# cursor/foo resolve under refs/heads/.
enc=$(printf '%s' "$branch" | sed 's/ /%20/g')
url="${PIADBLOCK_URL:-https://github.com/Bronsonalan/pi-adblocker/archive/refs/heads/${enc}.tar.gz}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "piadblock demo: python3 is required (ships with macOS)." >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then
  echo "piadblock demo: curl and tar are required." >&2
  exit 1
fi

tmpdir=$(mktemp -d)
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT

echo "piadblock demo: downloading $url"
curl -fsSL "$url" | tar -xz -C "$tmpdir"

src=$(find "$tmpdir" -mindepth 1 -maxdepth 1 -type d)
count=$(printf '%s\n' "$src" | wc -l | tr -d ' ')
if [ "$count" -ne 1 ] || [ ! -d "$src/piadblock" ]; then
  echo "piadblock demo: archive for branch '$branch' did not contain piadblock/" >&2
  exit 1
fi

rm -rf "$dest"
mkdir -p "$(dirname "$dest")"
mv "$src" "$dest"
echo "piadblock demo: installed $dest (branch $branch)"

cd "$dest"
# shellcheck disable=SC2086
exec python3 -m piadblock demo $demo_args
