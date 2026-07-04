#!/bin/bash
set -euo pipefail

SOURCE_ROOT="${REPO_HARNESS_SOURCE_ROOT:-${AGENTIC_DEV_ROOT:-${AGENTIC_DEV_SKILL_ROOT:-}}}"

if [[ -n "$SOURCE_ROOT" && -f "$SOURCE_ROOT/src/cli/index.ts" ]]; then
  if command -v bun >/dev/null 2>&1; then
    exec bun "$SOURCE_ROOT/src/cli/index.ts" run check-architecture-sync "$@"
  fi
fi

if command -v repo-harness >/dev/null 2>&1; then
  exec repo-harness run check-architecture-sync "$@"
fi

echo "Missing repo-harness CLI for helper check-architecture-sync" >&2
exit 1
