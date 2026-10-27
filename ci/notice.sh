#!/usr/bin/env bash
# Post a file's contents (or a command's output) as a notice on the workflow run,
# so results and debug output are readable from the run page and the API.
#   ci/notice.sh <title> <file>       |      ci/notice.sh <title> -- <command...>
set -uo pipefail
title=$1
shift
if [ "${1:-}" = "--" ]; then
  shift
  body=$("$@" 2>&1 | tail -n 40)
else
  body=$(tail -n 40 "$1" 2>/dev/null || echo "(no file $1)")
fi
echo "::notice title=${title}::$(printf '%s' "$body" | sed -e 's/%/%25/g' -e 's/\r//g' | awk '{printf "%s%%0A", $0}')"
