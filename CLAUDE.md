## Model Routing Rules

### Session model
- The main session runs on Fable 5 while this week's Fable quota remains, and on
  Opus 5 once it is exhausted. Every rule below applies identically in both
  cases; only the main session's model changes.
- Fable quota is the scarce resource. Never spend it on work a pinned subagent
  can do. It is reserved for the "never delegate" work below.
- Every subagent pins its own `model` and `effort` so its behaviour does not
  drift when the session model or session effort changes. Do not remove those
  pins, and do not set `CLAUDE_CODE_SUBAGENT_MODEL` — it overrides subagent
  frontmatter and would defeat the pinning.

### Never delegate; the main session handles these directly
- Architecture decisions, task breakdown, cross-module refactoring, final review.
- Trivial changes (a few lines in a single file); delegation overhead is not
  worth it.
- Changes touching these paths:
  - `apple-app/bundle-macos.sh` and `apple-app/release-ios.sh` (packaging /
    signing / secret red lines: `.p8` and `.env*` must never enter the bundle)
  - `backend/framework/mcp/tools/api_tool_factory.py` — the secrets handling
    parts (`read_secret` / `write_secret` / `.secrets.env`, 0600 perms)
  - `config.yaml` (`bundle_id` is an Apple-registered APNs topic; must not change)
- Governance documents: `CHARTER.md`, `EXECUTION_LEDGER.md`, `PROTOCOL.md`.
  These carry decision history and discipline, not boilerplate; "documentation"
  in chore-worker's scope means code-adjacent docs, never these. (`CHARTER.md`
  additionally requires explicit user consent to change at all.)
- Anything requiring sudo or changing system state (`scripts/wired-limit.sh`,
  LaunchDaemons, sysctl) — these end at the user's hands anyway.
- Anything that starts, stops, or bills paid compute (`colab/daily.sh`,
  `daily_serve_chain.sh`, colab CLI): spending units is a user-visible act.

### Classifier fallback watch (quality rule, not a cost rule)
- Cyber-adjacent work — the secrets handling in `api_tool_factory.py`, anything
  touching `.p8`, `.env*`, or file permissions — can trip a safety classifier and
  be answered by a weaker fallback model instead. Fable 5's cyber classifiers
  fire substantially more often than Opus 5's.
- Prefer starting that work in an Opus 5 session. If a fallback notice appears
  mid-task, stop and redo the change on Opus 5 rather than accepting the result.

### Single-GPU discipline (this machine is the production M4 Max 64G)
- One GPU, no preemption safety valve (MLX over-commit = page-thrash cliff).
  At most ONE real-model process at any moment: benches, inference servers
  (:14161), `devctl` real-model tests are mutually exclusive — with each other
  and with any delegated task that would start one.
- Long measurement runs (`bench_baseline.py` and successors) are main-session
  background tasks, never delegations: they are a single Bash command; a
  subagent wrapped around one adds a relay without adding judgment.
- Subagents must not start/stop services on :14159/:14160/:14161/:8080 or load
  models unless the delegation explicitly grants it. Golden tests
  (`backend/tests/`, FakeEngine, CPU-only) are always safe to run concurrently.

### Delegation targets
- `chore-worker`: running tests / lint / documentation / commit messages /
  implementing a small function from a detailed plan.
- `implementer`: multi-file feature implementation and moderately complex bug
  fixes.
- `reviewer`: first-pass review of a diff, before the main session signs off.
- `stage-reviewer`: post-stage full review (Opus 5 + xhigh, i.e. the ultracode
  thinking tier). Not a per-diff reviewer — it audits a whole completed stage:
  conclusions vs raw data, archive completeness, cross-document consistency,
  billing/secret paths. See "Stage review" below for when it is mandatory.
- Built-in `Explore` agent: broad read-only codebase reconnaissance that would
  otherwise flood the main context with file dumps. Pass a cheaper `model` in
  the Agent call (it has no frontmatter pin and would inherit the session
  model — i.e. burn Fable quota — if you don't).
- Delegate only genuinely self-contained work. Do not delegate what you can
  finish yourself in a handful of tool calls. If one subagent can complete the
  task, use one rather than several.
- Do not spawn a subagent to verify or double-check work another subagent just
  did. The two per-diff gates below and the stage review are the only
  verification steps.

### Delegation contract
- Every delegation must include: the specific file paths involved, and explicit
  acceptance criteria (which tests must pass).
- Include the environment facts the agent cannot discover and will get wrong:
  the venv is `backend/.venv` (the repo-root `.venv` is a dead symlink), test
  runs use `PYTHONUNBUFFERED=1`, and whether the GPU / inference ports are
  currently occupied by a running bench. State explicitly whether real-model
  execution is allowed (default: it is not).
- Hand over the complete specification up front rather than feeding it in
  stages.
- A task whose acceptance criteria you cannot write is not ready to delegate.
  Scope it first.

### Review gates — both mandatory, never collapsed to save usage
Scope: every non-trivial code diff, **including code the main session wrote
itself** (a 400-line harness deserves the same first pass as a delegated
change). Docs-only and ledger-only diffs are exempt.
1. `reviewer` produces the first pass on the diff.
2. The main session then reviews the diff itself before committing it or
   building further work on top of it. The main session owns the sign-off; the
   reviewer's report is input to that decision, not a replacement for it.
- Beyond reporting test results, do not instruct subagents to re-verify their own
  work. Redundant verification instructions cost tokens without adding quality.

### Stage review — mandatory after every completed stage (user decision, 2026-08-16)
- Trigger: a stage/station/wave has closed out — its commits are pushed and the
  closeout is being (or has just been) reported to the user. Before declaring
  the stage fully done, the main session launches `stage-reviewer` over the
  stage's entire commit range (plus any external repos touched).
- This is a third verification layer on top of the per-diff gates, at a
  different granularity: per-diff review checks the change; stage review audits
  the stage — conclusions recomputed from raw data, archive completeness
  (referenced artifacts actually in git), test-assertion validity, billing- and
  secret-path safety in ops scripts, and cross-document number consistency.
  Origin: a same-tier full review on 08-16 found 33 real defects in a stage
  that had already passed both per-diff gates.
- The delegation must include: the commit range, external repo paths/branches,
  environment facts (venv, GPU/port occupancy, what verification is allowed),
  and the stage's claimed conclusions (or where they are recorded).
- The stage-reviewer only reports; the main session verifies its findings
  (agents can be wrong — behavioral evidence beats fingerprints), applies the
  fixes, and runs the fixes through the normal per-diff gates. A stage is not
  closed until its stage review's confirmed findings are fixed or explicitly
  deferred with the user.
- Do not substitute the stage review for the per-diff gates or vice versa;
  do not run more than one stage review per stage (fix-up commits after the
  review do not re-trigger it — they go through the per-diff gates only).

### Escalation
- A task counts as failed if its acceptance criteria are not met.
- On the first failure, read the delegation itself before doing anything else,
  and classify it:
  - spec was incomplete or ambiguous → rewrite the spec, re-delegate once to the
    same agent
  - task is genuinely above the agent's level → escalate immediately
- Never re-run a delegation that already failed unchanged.
- Ladder: `chore-worker` → `implementer` → main session takes over directly.
- If a task turns out to need long-horizon autonomous work (30+ minutes,
  million-token budgets), it is out of `implementer`'s scope by definition:
  the main session either splits it or handles it.

### Phase-completion push (main session only)
- When a phase of work is finished and the turn ends waiting on the user's
  decision, push a notification: `reminder/notify.sh "<标题>" "<正文>"`.
  The user's phone is permanently on silent; this is the only channel that
  reaches them while they are away from the terminal.
- Applies to: a wave/phase closing, a long background run finishing, a blocking
  question that stalls all further work, and anything that spends or is about to
  spend paid compute. It does NOT apply to routine intermediate steps — a push
  per tool call trains the user to ignore pushes.
- Write the body so it is actionable without opening the laptop: what finished,
  what it means, and what decision (if any) is waiting. Not just "任务完成".
- Quiet hours 23:00–06:00: the script refuses to send (exit code 9) and appends
  the content to `reminder/suppressed.log` for the user to read the next morning.
  That is the designed behaviour, not a failure — do not retry it, do not widen
  the window with `BARK_QUIET_START/END` (debug-only knobs), and do not invent
  another channel to get around it. Just say in the final message that the push
  was suppressed by quiet hours.
- Never put secrets in the title or body; the token lives only in
  `reminder/bark.env` (0600, and `/reminder/` is gitignored). Never read, print,
  or commit that file — the script reads it itself and redacts the key even out
  of Bark's own error messages.
