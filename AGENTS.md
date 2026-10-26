# Codex project instructions

## Start here

This repository is the local-first desktop Agent product, not the coding assistant
maintaining it. Product `skills/` and `backend/framework/agents/` are runtime code;
Codex roles live in `.codex/agents/`. Keep these separate.

- Communicate with the user in Chinese unless they request otherwise.
- Before a substantive iteration, read `CHARTER.md`; it owns architecture principles.
  For protocol work also read `PROTOCOL.md` and the relevant fixtures and tests.
- Inspect `git status --short` and recent commits before changing files. Preserve
  unrelated changes; stage only explicit paths when a commit is authorized.
- Use `README.md` and `ARCHITECTURE.md` as maps, then verify relevant code. Some
  subproject READMEs describe old deployments. `docs/history/` is a frozen archive
  (2026-08-29), not today's task list. Later decisions and validation live in commit
  messages. Consult `DEFERRED.md` and `IDEAS.md` only when relevant; verify status.
- Do not rewrite the charter or thaw history as routine documentation work.
  Architecture-direction changes need explicit user agreement.

## Architecture boundaries

Preserve the charter's invariants: journal before event delivery; `TurnRunner` as
the single turn-lifecycle owner; thin HTTP adapters; Web/Swift shared protocol
fixtures; bounded memory; deterministic guardrails and a single approval gate;
sandbox isolation; cloud advice never directly executes tools; egress decisions
remain separate from task execution. Read relevant golden tests before changing
orchestration, memory, scheduling, inference, or sandbox behavior.

`config.yaml` is the product's single configuration source. `devctl.sh` is the
standard development entry point; `scripts/lib_edge.sh` is shared with the app.
Do not duplicate startup logic, renderers, or test gates in Codex configuration.

## Environment and operational boundaries

- Run `./devctl.sh preflight` before implementation delegation or environment-
  dependent validation. Pass its actual JSON to the delegate; do not hand-copy
  machine assumptions. For read-only research with no execution, scoped paths and
  an explicit no-execution instruction are sufficient.
- `unknown`, `not_rejected`, and missing observations are not success. Explain how
  each preflight uncertainty affects this task. CPU-only tests do not require a
  GPU-free claim; leave GPU status unknown when it is irrelevant.
- At most one real-model inference/benchmark process per GPU. Do not start, stop,
  download, or load real models without task authorization and an occupancy check.
  Multiple coding subagents do not grant permission for concurrent GPU work.
- Do not start persistent development services as a side effect of a code change.
  Before stopping a service, verify process identity, ownership and task scope;
  never kill everything on a port. This is the user-approved Codex maintenance
  replacement (2026-10-09) for the charter's old port-wide pre-test kill recipe.
- Packaging/signing, `config.yaml`, API-tool secret handling, system settings and
  paid compute stay with the main agent. Preserve the APNs `bundle_id`. Changes to
  these paths do not themselves authorize deployment, key rotation or spending.
- Never read or print live credentials (`.env.local`, `.secrets.env`, `.keys/`,
  signing keys, `reminder/bark.env`, auth stores). Sanitized tracked templates and
  synthetic fixtures may be read. Authorized local scripts may consume credentials
  without exposing values. Use presence, fingerprints or redacted diagnostics.
- Keep credentials and user data out of prompts, logs, bundles and Git. Avoid
  full process command lines when they may contain tokens.

## Delegation and model routing

Use subagents for the scoped roles below. This section explicitly requests
delegation for the review and monitoring gates; the user need not repeat that
request each time. The main agent owns planning, architectural choices, final
review, integration and user communication. Trivial edits stay with the main agent.

| Role | Model / effort | Scope |
|---|---|---|
| explorer | gpt-6-luna / high | Read-only code and history investigation |
| chore-worker | gpt-6-luna / high | Precisely scoped small edits, docs, named tests |
| implementer | gpt-6.1-sol / high | Bounded multi-file implementation and fixes |
| reviewer | gpt-6.1-sol / high | Independent first-pass review of non-trivial changes |
| stage-reviewer | gpt-6-astra / xhigh | Whole-stage evidence and consistency audit |
| task-sentinel | gpt-6-luna / low | Read-only monitoring of long-running work |

The role TOMLs are authoritative for role instructions and model settings. The
main model is inherited from the user's current selection. Do not silently replace
an unavailable role model; report the limitation and agree an alternative.

Use native named roles when the client exposes them. If the available spawn tool
has no role selector, read the corresponding TOML and pass its instructions and
explicit model/effort in a fresh or bounded-context delegation. Do not claim that
this fallback applies the role's sandbox defaults: verify the live permissions and
enforce the role's no-edit/no-service restrictions. Never work around a denied
capability by switching execution surfaces.

Every implementation/review delegation names the owned files or diff, goal,
acceptance criteria, allowed commands, preflight evidence and relevant unknowns.
Real-model execution defaults to forbidden. At most three concurrent subagents;
one writer per file; do not edit a file while another agent validates its diff.
Subagents do not delegate further, commit, push, release or notify the user.

For failure, inspect the specification first. Clarify an incomplete specification;
otherwise escalate chore-worker → implementer → main agent. Never repeat an
unchanged failed delegation. Do not add redundant verification agents beyond the
first-pass and stage gates.

## Validation and review

- Protocol behavior: fixtures first, then specification and implementation;
  changing existing field semantics requires protocol-version review.
- During development run the targeted tests. Before completing a code change run
  `./devctl.sh check`: backend golden tests, Web tests, Swift reducer fixtures.
  Missing prerequisites are a blocked check, not a pass. Never use `--no-verify`,
  `-n`, disable hooks, or silently skip a gate to get a green result.
- For affected frontend build paths also run the relevant build/lint; for native
  changes run relevant build checks. Real-model/E2E tests require the operational
  conditions above. Report unavailable validation honestly.
- Non-trivial executable/configuration changes, including the main agent's own:
  request one independent `reviewer`, then main-agent verification of findings and
  final diff. Pure prose changes do not require this gate.
- Guard changes require mutation proof: deliberately remove the protected behavior
  and demonstrate the intended test fails. Prefer an isolated temporary copy.
  If modifying the worktree temporarily, restore byte-for-byte in `finally`, verify
  hashes, and do not overlap with other writers. Invalidate only affected Python
  bytecode, not arbitrary repository caches. Record proof in the eventual commit
  message, or the completion report when no commit is requested.
- Before declaring a whole stage complete, request one `stage-reviewer` over its
  commit range (or baseline + complete tracked/untracked diff if uncommitted).
  Include claimed results and evidence locations. Resolve confirmed findings or
  obtain explicit deferral. Fixes receive ordinary diff review, not another full
  stage audit. Prefer audit before publishing/releasing the stage.
- Tests create caches/temp artifacts: a read-only reviewer should inspect code and
  supplied evidence, requesting main-agent test execution when its live sandbox
  cannot run the needed test. Do not relax its sandbox just to rerun tests.

## Long tasks, delivery and notifications

For any run expected to exceed ten minutes, request `task-sentinel` when launching
it. The main agent owns the run and its tracked execution handle. Give the sentinel
a PID/handle, log paths, expected cadence, stale threshold, deadline and benign
failure patterns. Use bounded waits and approximately three-minute checks; read
log increments. No `nohup`/detached monitor assumed to wake Codex automatically.
If reliable delegated monitoring is unavailable, the main agent retains monitored
execution and says so; never leave a long run unobserved.

Continue authorized reversible work without repeated permission questions. Commit,
push, release, external notifications, service changes and paid compute follow
the current task's authorization; historical approvals are not blanket permission.
Use `codex/` for new branches when a branch is needed. Do not fabricate co-authors
or Claude session links. Do not create a second maintenance ledger.

Report outcome, changed files, tests actually run, limitations and remaining user
actions. Distinguish files written, configuration parsed, roles loaded and hooks
trusted/active. New or changed non-managed Codex hooks require client trust review;
inspect `/hooks` in CLI, never bypass trust or edit its trust store yourself.

Use normal in-chat updates. If an external completion notification is authorized,
send at most once after the entire task is verified, or when blocked on the user;
never from a subagent or phase boundary. Honor the existing 23:00–06:00 quiet hours
and suppressed-notification behavior; do not add a duplicate Codex notify hook.

## Configuration provenance

Codex adaptation approved 2026-10-09: retains independent reviews, mutation proof,
single-GPU discipline and commit guards from `CLAUDE.md`, while replacing Claude
quota routing/background-wakeup assumptions and stale machine facts. Claude's
configuration remains in service for Claude; Codex follows this entry point.
Official formats checked against Codex CLI 0.162.0-alpha.2 and:
- https://learn.chatgpt.com/docs/agent-configuration/agents-md
- https://learn.chatgpt.com/docs/agent-configuration/subagents
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://learn.chatgpt.com/docs/hooks
