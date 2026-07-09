#!/bin/bash
# Fast pre-commit checks for swf-monitor — target: under ten seconds.
#
# Encodes the exact invocations (venv, env, manage.py) so no session has to
# rediscover them. Checks only what changed: compiles modified .py files,
# runs the Django system check once, and lints modified templates for the
# two known silent page-breakers (unterminated one-line {# #} comments,
# unbalanced double quotes on tag-bearing lines).
#
# Usage: bash scripts/pre-commit-checks.sh   (from anywhere in the repo)
set -e
cd "$(dirname "$0")/.."

source /data/wenauseic/github/swf-testbed/.venv/bin/activate
# shellcheck disable=SC1090
source ~/.env 2>/dev/null || true

CHANGED=$( (git diff --name-only HEAD 2>/dev/null; \
            git diff --cached --name-only 2>/dev/null; \
            git ls-files --others --exclude-standard) | sort -u )

PY_CHANGED=$(echo "$CHANGED" | grep '\.py$' || true)
HTML_CHANGED=$(echo "$CHANGED" | grep '\.html$' || true)

if [ -n "$PY_CHANGED" ]; then
    # shellcheck disable=SC2086
    python -m py_compile $PY_CHANGED
    echo "compile: OK ($(echo "$PY_CHANGED" | wc -l) changed .py)"
else
    echo "compile: no .py changes"
fi

python src/manage.py check 2>&1 | tail -1

FAIL=0
for f in $HTML_CHANGED; do
    [ -f "$f" ] || continue
    if grep -n '{#' "$f" | grep -v '#}' > /dev/null; then
        echo "FAIL: unterminated {# comment in $f:"
        grep -n '{#' "$f" | grep -v '#}'
        FAIL=1
    fi
    python3 - "$f" << 'PYEOF'
import sys
bad = [i for i, line in enumerate(open(sys.argv[1]), 1)
       if '<' in line and line.count('"') % 2]
if bad:
    print(f"WARN: odd double-quote count in {sys.argv[1]} "
          f"lines {bad} — check for an unterminated attribute")
PYEOF
done
[ "$FAIL" -eq 0 ] || exit 1

echo "pre-commit checks passed"
