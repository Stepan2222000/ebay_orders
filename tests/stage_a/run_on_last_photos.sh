#!/usr/bin/env bash
# Run Stage A CLI on the first N (default 5) screenshots from last_photos/.
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${PROJECT_ROOT}"

N="${N:-5}"
FILES=()
while IFS= read -r line; do FILES+=("$line"); done < <(ls last_photos/*.png | head -n "$N")

PASS=0
FAIL=0
for f in "${FILES[@]}"; do
    echo "============================================================"
    if python3 scripts/run_stage_a.py "$f"; then
        PASS=$((PASS+1))
    else
        FAIL=$((FAIL+1))
    fi
done
echo "============================================================"
echo "${PASS} ok, ${FAIL} failed (out of ${#FILES[@]})"
exit $FAIL
