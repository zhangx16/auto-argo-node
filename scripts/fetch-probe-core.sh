#!/bin/sh
set -eu
VERSION=1.14.3
case "$(uname -m)" in x86_64|amd64) ARCH=amd64;; aarch64|arm64) ARCH=arm64;; armv7l) ARCH=armv7;; *) echo 'Unsupported probe architecture';exit 1;; esac
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
ASSET="sing-box-${VERSION}-linux-${ARCH}.tar.gz"
curl --proto '=https' -fsSL --retry 3 --max-time 90 "https://api.github.com/repos/SagerNet/sing-box/releases/tags/v${VERSION}" -o "$TMP/release.json"
python - "$TMP/release.json" "$ASSET" "$TMP/info" <<'PY'
import json,sys,re
items=[x for x in json.load(open(sys.argv[1]))['assets'] if x['name']==sys.argv[2]]
assert len(items)==1
asset=items[0];digest=asset.get('digest','')
assert re.fullmatch('sha256:[a-f0-9]{64}',digest)
assert asset['browser_download_url'].startswith('https://github.com/SagerNet/sing-box/releases/download/')
open(sys.argv[3],'w').write(asset['browser_download_url']+'\n'+digest[7:]+'\n')
PY
URL=$(sed -n '1p' "$TMP/info");SHA=$(sed -n '2p' "$TMP/info")
curl --proto '=https' -fsSL --retry 3 --max-time 180 "$URL" -o "$TMP/core.tar.gz"
printf '%s  %s\n' "$SHA" "$TMP/core.tar.gz" | sha256sum -c -
tar -xzf "$TMP/core.tar.gz" -C "$TMP"
install -m 755 "$TMP/sing-box-${VERSION}-linux-${ARCH}/sing-box" /usr/local/bin/sing-box-probe
