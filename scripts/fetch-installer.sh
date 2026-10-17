#!/bin/sh
# Fetch, verify, then install the exact upstream dependency; never execute the download here.
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
DEST="$ROOT/app/vendor/singbox.sh"
COMMIT=5486a730555ee96730791062aefd440a7b892751
SHA256=86687380b45b765f8a8d79e073d5c0972c7175497b995e4099c78091a0ae1cf4
mkdir -p "$ROOT/app/vendor"
TMP=$(mktemp "$ROOT/app/vendor/.installer.XXXXXXXX")
trap 'rm -f "$TMP"' EXIT HUP INT TERM
curl --proto '=https' --tlsv1.2 -fSL --retry 3 --connect-timeout 15 --max-time 120 \
 "https://raw.githubusercontent.com/0xdabiaoge/singbox-lite/$COMMIT/singbox.sh" -o "$TMP"
printf '%s  %s\n' "$SHA256" "$TMP" | sha256sum -c -
chmod 644 "$TMP"
mv "$TMP" "$DEST"
