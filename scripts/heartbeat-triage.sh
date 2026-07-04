#!/bin/bash
set -euo pipefail

SOURCE_ROOT="${REPO_HARNESS_SOURCE_ROOT:-${AGENTIC_DEV_ROOT:-${AGENTIC_DEV_SKILL_ROOT:-}}}"

if [[ -n "$SOURCE_ROOT" && -f "$SOURCE_ROOT/src/cli/index.ts" ]]; then
  if command -v bun >/dev/null 2>&1; then
    exec bun "$SOURCE_ROOT/src/cli/index.ts" run heartbeat-triage "$@"
  fi
fi

if command -v repo-harness >/dev/null 2>&1; then
  exec repo-harness run heartbeat-triage "$@"
fi

echo "Missing repo-harness CLI for helper heartbeat-triage" >&2
exit 1
