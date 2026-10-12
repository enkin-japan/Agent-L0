#!/bin/bash
# Compatibility entry point: Claude and Codex share one tested implementation.
# Resolve from this file so subdirectory and linked-worktree invocations work.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)" || exit 2
exec bash "${repo_root}/scripts/agent-hooks/block-no-verify.sh"
