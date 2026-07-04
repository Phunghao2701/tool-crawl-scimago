#!/bin/bash
set -euo pipefail

SOURCE_ROOT="${REPO_HARNESS_SOURCE_ROOT:-${AGENTIC_DEV_ROOT:-${AGENTIC_DEV_SKILL_ROOT:-}}}"

if [[ -n "$SOURCE_ROOT" && -f "$SOURCE_ROOT/src/cli/index.ts" ]]; then
  if command -v bun >/dev/null 2>&1; then
    exec bun "$SOURCE_ROOT/src/cli/index.ts" run contract-worktree "$@"
  fi
fi

if command -v repo-harness >/dev/null 2>&1; then
  exec repo-harness run contract-worktree "$@"
fi

echo "Missing repo-harness CLI for helper contract-worktree" >&2
exit 1
