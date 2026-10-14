#!/bin/sh
# Download piadblock and install it. Raspberry Pi OS already has curl, tar, and python3.
#   curl -fsSL https://raw.githubusercontent.com/Bronsonalan/pi-adblocker/main/bootstrap.sh | sudo bash

set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "piadblock: this installer needs root." >&2
  echo "Run: curl -fsSL https://raw.githubusercontent.com/Bronsonalan/pi-adblocker/main/bootstrap.sh | sudo bash" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "piadblock: python3 is required. It is preinstalled on Raspberry Pi OS." >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1 || ! command -v tar >/dev/null 2>&1; then
  echo "piadblock: curl and tar are required. They ship with Raspberry Pi OS." >&2
  exit 1
fi

url="https://github.com/Bronsonalan/pi-adblocker/archive/refs/heads/main.tar.gz"
tmpdir=$(mktemp -d)
cleanup() {
  rm -rf "$tmpdir"
}
trap cleanup EXIT

echo "piadblock: downloading $url"
curl -fsSL "$url" | tar -xz -C "$tmpdir"

src=$(find "$tmpdir" -mindepth 1 -maxdepth 1 -type d)
count=$(printf '%s\n' "$src" | wc -l)
if [ "$count" -ne 1 ] || [ ! -f "$src/install.py" ]; then
  echo "piadblock: the downloaded archive did not contain install.py" >&2
  exit 1
fi

echo "piadblock: running installer"
python3 "$src/install.py"
