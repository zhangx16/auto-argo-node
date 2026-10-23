#!/bin/sh
# Build on the panel/build host. No compiler is installed on protocol nodes.
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
OUT="$ROOT/artifacts/releases"
mkdir -p "$OUT" "$ROOT/artifacts/go-cache"
for spec in 1.14.3:7054cac5124465ffdb145870d7c01885566711b1 1.13.21:628cb31ffa79cffffd34c2f9cde6cae044e4fc12;do
 version=${spec%%:*};rev=${spec#*:};src="$ROOT/artifacts/sing-box-$version"
 [ -d "$src/.git" ] || git clone https://github.com/SagerNet/sing-box.git "$src"
 git -C "$src" checkout --detach "$rev"
 for arch in amd64 arm64;do
  name="sing-box-minimal-$arch";[ "$version" = 1.14.3 ] || name="sing-box-minimal-$version-$arch"
  docker run --rm --cpus=2 --memory=2g -v "$src:/src:ro" -v "$ROOT/artifacts/go-cache:/go" -v "$OUT:/out" -w /src \
   -e CGO_ENABLED=0 -e GOARCH="$arch" -e CORE_VERSION="$version" -e CORE_OUTPUT="$name" -e GOTOOLCHAIN=local -e GOMAXPROCS=2 golang:1.26.8-bookworm \
   sh -c 'go build -p 2 -trimpath -tags with_quic,with_utls,badlinkname,tfogo_checklinkname0 -ldflags "$(cat release/LDFLAGS) -s -w -X github.com/sagernet/sing-box/constant.Version=$CORE_VERSION" -o "/out/$CORE_OUTPUT" ./cmd/sing-box'
 done
done
python3 "$ROOT/scripts/prepare-core-releases.py"
