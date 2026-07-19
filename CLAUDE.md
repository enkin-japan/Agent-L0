## Model Routing Rules
- Architecture decisions, task breakdown, cross-module refactoring, and final
  review: handled directly by the main session. Never delegate these.
- Running tests / lint / documentation / commit messages / implementing a
  small function from a detailed plan: delegate to chore-worker.
- Multi-file feature implementation and moderately complex bug fixes:
  delegate to implementer.
- Every delegation must include: the specific file paths involved, and
  explicit acceptance criteria (which tests must pass).
- After a subagent completes a task that modified code, the main session must
  review the resulting diff before committing it or building further work on
  top of it.
- Escalation: a task counts as failed if its acceptance criteria are not met.
  If chore-worker fails the same task twice, escalate it to implementer. If
  implementer fails it, the main session takes over directly.
- Changes touching the following paths must never be delegated; the main
  session handles them directly:
  - apple-app/bundle-macos.sh and apple-app/release-ios.sh (packaging /
    signing / secret red lines: .p8 and .env* must never enter the bundle)
  - backend/framework/mcp/tools/api_tool_factory.py — the secrets handling
    parts (read_secret / write_secret / .secrets.env, 0600 perms)
  - config.yaml (bundle_id is an Apple-registered APNs topic; must not change)
- Do not delegate trivial changes (a few lines in a single file); the main
  session does them directly, as delegation overhead is not worth it.
