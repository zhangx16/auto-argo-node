#!/bin/sh
# Pinned upstream sources plus the reviewed local integration patch (MIT).
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
OUT="$ROOT/artifacts/monitor-build"
mkdir -p "$OUT" "$ROOT/artifacts/releases"
for spec in hub:monitor:e8b3cfea64f10c908bfc5325bab45d3ea0b02a0b agent:agent:e240c66dc99d3106928c41973cb3655dc74a8461;do
 name=${spec%%:*};rest=${spec#*:};repo=${rest%%:*};rev=${rest#*:}
 [ -d "$OUT/$name/.git" ] || git clone "https://github.com/monitor-probe/$repo.git" "$OUT/$name"
 git -C "$OUT/$name" checkout --detach "$rev"
 git -C "$OUT/$name" restore src
 done
python3 "$ROOT/integrations/monitor/patch.py" "$OUT/hub" "$OUT/agent"
(cd "$OUT/hub/web-admin" && npm ci --ignore-scripts && npm run build)
for name in hub agent;do
 (cd "$OUT/$name" && CARGO_BUILD_JOBS=2 cargo build --release --locked --target x86_64-unknown-linux-musl)
 cp "$OUT/$name/target/x86_64-unknown-linux-musl/release/monitor-$name" "$ROOT/artifacts/releases/monitor-$name-amd64"
done
# ARM requires cargo-zigbuild/zig and the aarch64 Rust target on the build host.
(cd "$OUT/agent" && CARGO_BUILD_JOBS=2 cargo zigbuild --release --locked --target aarch64-unknown-linux-musl)
cp "$OUT/agent/target/aarch64-unknown-linux-musl/release/monitor-agent" "$ROOT/artifacts/releases/monitor-agent-arm64"
