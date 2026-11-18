#!/usr/bin/env bash
# Reproduce the full admission-evidence set against an ephemeral service.
# Production source and limits are never modified; everything lives under
# tests/admission and uses throwaway data roots.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
GO_BIN="${GO_BIN:-go}"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

echo "[1/4] verify production build and vet"
( cd "$ROOT" && "$GO_BIN" build ./... && "$GO_BIN" vet ./... )

echo "[2/4] regenerate deterministic matrix manuscripts"
python3 -I "$HERE/generate_cases.py"

echo "[3/4] build independent cost probe and collect static + measured evidence"
( cd "$ROOT" && "$GO_BIN" build -o "$BUILD_DIR/costprobe" ./tests/admission/costprobe )
for f in "$HERE"/cases/*.json; do
  n="$(basename "$f" .json)"
  [ "$n" = "manifest" ] && continue
  "$BUILD_DIR/costprobe" -mode measure -case "$f" \
    -out "$HERE/evidence/raw-probe-$n.json" -repeats 3 -timeout-ms 20000
done
# Bounded-deadline evidence: same extreme manuscript, 2s probe deadline.
"$BUILD_DIR/costprobe" -mode measure \
  -case "$HERE/cases/c1-curve-extreme-512.json" \
  -out "$HERE/evidence/raw-probe-c1-timeout2s.json" \
  -repeats 1 -timeout-ms 2000

echo "[4/4] real HTTP matrix (create, revision PUT, bundle import, body guard, cancel, cross-instance)"
python3 -I "$HERE/run_admission.py" --go "$GO_BIN" \
  --probe "$BUILD_DIR/costprobe" --out "$HERE/evidence"

python3 -I "$HERE/aggregate.py"
echo "evidence written to $HERE/evidence (raw JSON + SUMMARY.md + aggregate.json)"
