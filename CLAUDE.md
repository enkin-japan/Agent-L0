## Model Routing Rules

### Session model
- The main session runs on Fable 5 while this week's Fable quota remains, and on
  Opus 5 once it is exhausted. Every rule below applies identically in both
  cases; only the main session's model changes.
- Fable quota is the scarce resource. Never spend it on work a pinned subagent
  can do. It is reserved for the "never delegate" work below.
- Every subagent pins its own `model` and `effort` (`.claude/agents/*.md`) so
  its behaviour does not drift when the session model or session effort
  changes. Do not remove those pins. Every `Agent` call that has no frontmatter
  pin (built-in `Explore`, `general-purpose`) names an explicit `model`; none
  may inherit the session model.
- Safety net (behaviour verified in Agent-L1 on 2026-09-28, CLI 2.1.282):
  `.claude/settings.json` sets `CLAUDE_CODE_SUBAGENT_MODEL=claude-opus-5-5`, so
  an agent without a pin falls to Opus instead of the session model; pins still
  win. Do not change or unset it, and never set
  `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` (that one would override the pins).

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
- Governance documents: `CHARTER.md`, `PROTOCOL.md`, and the frozen
  `docs/history/EXECUTION_LEDGER.md` (archived 2026-08-29; read-only).
  These carry decision history and discipline, not boilerplate; "documentation"
  in chore-worker's scope means code-adjacent docs, never these. (`CHARTER.md`
  additionally requires explicit user consent to change at all.)
- Anything requiring sudo or changing system state (`scripts/wired-limit.sh`,
  LaunchDaemons, sysctl) — these end at the user's hands anyway.
- Anything that starts, stops, or bills paid compute (`colab/deploy.sh` and the
  `colab/compute_*.py` chain behind it, the archived `colab/legacy/` scripts, colab
  CLI, and any script that wraps a lease such as
  `benchmarks/w3_vision_smoke/run_colab.sh`): spending units is a user-visible act.

### Classifier fallback watch (quality rule, not a cost rule)
- Cyber-adjacent work — the secrets handling in `api_tool_factory.py`, anything
  touching `.p8`, `.env*`, or file permissions — can trip a safety classifier and
  be answered by a weaker fallback model instead. Fable 5's cyber classifiers
  fire substantially more often than Opus 5's.
- Prefer starting that work in an Opus 5 session. If a fallback notice appears
  mid-task, stop and redo the change on Opus 5 rather than accepting the result.

### What the main session does not do
Every main-session call re-reads the whole context; a command run here costs
two to three times what it costs in a subagent, and its output stays in the
context for the rest of the session.
- No test suites, gates, `xcodebuild`, or mutation harnesses in the foreground:
  `verifier`. (The pre-commit hook and one named test during final review are
  fine.)
- No reconnaissance: repo-wide searches, reading files to get oriented:
  `scout`, or built-in `Explore` with a cheaper `model` for pure location.
- No reading of raw run artifacts or logs, no inline recomputation: `analyst`.
- No experiment drivers, probes, or harness scripts: `implementer`.
- No waiting or polling: background the task, start the watch loop (see "Long
  tasks" below), end the turn.
- Never read a subagent's transcript or raw task output; its report is the
  interface.
- Rule of thumb: more than three tool calls, or more than a hundred lines
  coming back: delegate.

### Long tasks (user decision 2026-10-31; replaces "attach task-sentinel" of 2026-08-16)
- Any task expected to run longer than 10 minutes gets a watch loop started
  in the same action sequence that launches it: a second Bash call with
  `run_in_background: true` running `scripts/watch_until.sh` (process
  liveness, log/command patterns for done and fail, stall threshold, hard
  deadline; prints one `EXIT_<STATE>` line and exits). Its exit wakes the main
  session directly. No model sits between the loop and the wake-up.
- Why: the sentinel agent used to start that loop itself. In 2 of 26 runs this
  session it detached the loop with a bare `&` and handed back after 40–60 s;
  the loop's exit then woke nobody. Once the main session waited 1.5 h for a
  finished commit; once an A100 sat ready for 46 minutes until the lease
  expired, with the acceptance probe never sent. Both failures were invisible
  in the sentinel's report. The loop needs no judgement; the launch mechanism
  needs precision — the wrong thing to ask of the cheapest model.
- Paid compute (Colab) never depends on a wake-up at all: the consuming action
  is chained into the same background script as the lease — start, wait for
  READY, run, stop unconditionally (`benchmarks/w3_vision_smoke/run_colab.sh`
  is the template). The watch loop then only reports.
- A watch loop whose exit needs interpretation (anomaly, stall, partial
  output) may be handed to `task-sentinel` for triage; a plain EXIT_DONE the
  main session reads itself.

### Context discipline
- Working budget for the main session: 180k tokens. At a phase boundary above
  the budget, persist (plan file, memory) and ask the user to `/compact`.
  (The harness auto-compacts at about 230k — observed 229k and 250k on
  2026-10-30/31 — so the old 400k figure never triggered this rule; the
  harness then idle-compacts "while you were away" and applies the summary
  when the user's next message arrives.)
- Compact before leaving, not after returning: when a work window ends and
  everything is persisted, ask for `/compact` while the cache is warm.
- Output that may exceed 60 lines goes to a file; read the tail or a summary.
  No full logs, no long-line diffs in the context (`bashOutputMaxChars` in
  `.claude/settings.json` caps what a Bash call can return).
- Subagent reports are at most 60 lines; evidence goes to files and the report
  gives paths.
- Unattended windows cannot be compacted: size the slice to the budget before
  starting.

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
- Subagents must never run process-killing or service-restart experiments on
  this machine (`pkill`/`kill` patterns, restarting anything), not even to
  "verify" a finding: a verify agent killed the user's Ollama.app server on
  2026-09-11 this way (Agent-L1 incident, same machine).

### Delegation targets
- `chore-worker` (Sonnet 5, high): small edits, lint, formatting, code-adjacent
  documentation, commit message drafts, implementing a small function from a
  detailed plan.
- `implementer` (Opus 5.5, high): multi-file feature implementation,
  moderately complex bug fixes, experiment drivers and harness scripts.
- `reviewer` (Opus 5.5, high): first-pass review of a diff, before the main
  session signs off; skeptic for a Critical / Major finding.
- `scout` (Opus 5.5, high): read-only reconnaissance with verified `path:line`
  citations, before design decisions.
- `analyst` (Opus 5.5, high): recomputation from raw run artifacts, analysis
  scripts under `benchmarks/`, draft result paragraphs.
- `verifier` (Sonnet 5, high): runs tests, gates, `xcodebuild`, mutation
  harnesses; one at a time; reports a table, fixes nothing.
- `stage-reviewer` (Opus 5.5, xhigh, i.e. the ultracode thinking tier):
  post-stage full review. Not a per-diff reviewer — it audits a whole completed
  stage: conclusions vs raw data, archive completeness, cross-document
  consistency, billing/secret paths. See "Stage review" below for when it is
  mandatory.
- `task-sentinel` (Haiku 4.5, low): triage only, after a watch loop has
  exited and woken the main session — reads incremental evidence, returns one
  status word with evidence lines. It no longer watches anything (user
  decision 2026-10-31; see "Long tasks" below for why). Optional: for a plain
  EXIT_DONE the main session reads the EXIT line itself.
- Built-in `Explore` agent: broad read-only codebase reconnaissance that would
  otherwise flood the main context with file dumps. Pass a cheaper `model` in
  the Agent call (it has no frontmatter pin and would inherit the session
  model — i.e. burn Fable quota — if you don't).
- Delegate only genuinely self-contained work. Do not delegate what you can
  finish yourself in a handful of tool calls. If one subagent can complete the
  task, use one rather than several.
- Do not spawn a subagent to verify or double-check work another subagent just
  did. The two per-diff gates below and the stage review are the only
  verification steps. The one exception is a skeptic (`reviewer`) per
  Critical / Major finding in a review or stage review.
- Agent batches of at most ten that spend no Fable need one line of notice in
  the reply; larger batches, or batches that spend Fable, need the user's go
  first.

### Delegation contract
- Every delegation must include: the specific file paths involved, and explicit
  acceptance criteria (which tests must pass).
- Include the environment facts the agent cannot discover and will get wrong.
  **Do not hand-copy them — run `./devctl.sh preflight` and paste the JSON.**
  It reports venv paths and the exact test command, port tri-states, GPU
  occupancy, which keys are auto-verified vs manual, and the active model / RAG
  tier. Hand-copied facts go stale silently: this rule used to state that the
  repo-root `.venv` is a dead symlink, and by 2026-08-18 there was no
  repo-root `.venv` at all.
  Two things preflight cannot decide for you, so state them yourself:
  - **whether real-model execution is allowed** (default: it is not);
  - what you do about each entry in `needs_confirmation` — GPU occupancy reads
    `unknown` rather than `free` whenever there is no positive signal, because
    an in-process MLX bench holds neither a port nor a pidfile. Confirm it
    yourself and record the decision in `waived`.
- Hand over the complete specification up front rather than feeding it in
  stages.
- A task whose acceptance criteria you cannot write is not ready to delegate.
  Scope it first.
- State the report contract: at most 60 lines, evidence in files, paths in the
  report.
- Read scope (user decision 2026-09-29, recorded in Agent-L1; the point is to
  keep the user's personal data from leaking). No request needed: this
  repository, the sibling repo `/Volumes/enkin/project/Agent-L1`, Claude Code's
  own working directories, and dependency libraries / toolchains / machine state
  / configuration that hold no personal data. Anything that may hold personal
  data (documents, desktop, downloads, mail, messages, photos, browser data,
  cloud drives, other projects' directories), or that you are unsure about, is
  read only after the main session has reported the exact paths to the user and
  received the go; a delegation lists those authorized paths, and the agent
  reads nothing of that kind outside the list. Secrets and credentials are never
  read, wherever they are; the user's notes, sessions and rag data are looked
  at by name and size only.
- Parallel implementers work in separate worktrees; anything that rewrites
  source files (mutation harnesses) or builds the same sources (`xcodebuild`)
  runs one at a time.

### Review gates — both mandatory, never collapsed to save usage
Scope: every non-trivial code diff, **including code the main session wrote
itself** (a 400-line harness deserves the same first pass as a delegated
change). Docs-only and ledger-only diffs are exempt.
1. `reviewer` produces the first pass on the diff.
2. The main session then reviews the diff itself before committing it or
   building further work on top of it. The main session owns the sign-off; the
   reviewer's report is input to that decision, not a replacement for it.
- Gate 2 reads by risk, and the commit message says which way it read:
  - line by line: guards, secret / permission / file handling, packaging and
    signing scripts, state machines and concurrency, acceptance criteria and
    test oracles, everything the reviewer flagged;
  - by summary (`--stat`, the reviewer's report, two or three sampled hunks):
    pure moves, deletions, generated files, fixtures, documents.
- Beyond reporting test results, do not instruct subagents to re-verify their own
  work. Redundant verification instructions cost tokens without adding quality.

### Mutation proof — required for guard-type changes (single source of truth)
A guard that cannot fail is not a guard. Before a guard-type change lands,
inject a mutant the guard is supposed to catch and confirm the intended test
actually goes red. "This test would catch it" is reasoning, not evidence; a red
test is evidence.
- **Scope is narrow on purpose: guard-type changes only** — golden tests,
  guardrail branches, fail-closed decisions, protocol fixtures. Ordinary feature
  code and refactors are out of scope. A proof obligation on every diff turns
  into sessions spent fighting the gate, and gates that are fought get deleted.
- **This applies repo-wide, not just to protocol fixtures.** It was written down
  for fixtures first (`protocol/fixtures/README.md`), but both observed failures
  since were in the backend guard domain: `_load_drafter`'s mock sat so deep that
  no mutant could be killed through it, and the C1 streaming-BPE defect that
  stage-reviewer caught on its first run.
- Restore the source byte-for-byte afterwards and verify the restore. A mutation
  run that leaves the tree modified is worse than not running one.
- **If a mutant survives, suspect the mutant — and the fixture — before the
  assertion.** A mutant that does not faithfully reproduce the defect proves
  nothing, and neither does one the test harness never actually loaded. Only
  once both are confirmed does a survivor mean the assertion is vacuous.
- **Clear `__pycache__` between injection and test run.** Bytecode caching keys
  on `(mtime seconds, source size)`, and mutants are routinely the same size and
  land inside the same second (`== 1` → `== 0`). The run then executes
  pre-mutation bytecode and the mutant "survives" for a reason that has nothing
  to do with the assertion. Observed 2026-08-18 in the soak-harness wave; the
  loader-side fix in one test file does not generalize, so the clearing belongs
  in the ritual: `find . -name __pycache__ -type d -not -path "./.venv/*" -exec
  rm -rf {} +` before each mutant's test run.
- Record the proof in the commit message of the change (which mutants, which
  tests went red). Until 2026-08-29 the record went into that wave's
  `EXECUTION_LEDGER.md` entry; the ledger is now frozen in `docs/history/`, and
  the commit message is the successor record (user decision 2026-08-29).
  **There is no separate mutation ledger** — a second place to keep the same
  numbers in sync is just a third source of drift.
- Who runs it: execution belongs to `verifier`, in series, one harness at a
  time; it returns the mutant → red tests → restored table and
  `git status --short`. For a guard on one of the "never delegate" red-line
  paths the main session names the mutants and re-runs one of them itself.

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
- Every Critical / Major finding of a stage review gets one skeptic
  (`reviewer`, default stance: refute it) before the main session verifies the
  survivors.
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

### Completion-or-blocked push ONLY (user decision 2026-08-16; replaces the old phase-completion rule after a 4-pushes-in-20-minutes incident)
- Push a notification (`reminder/notify.sh "<标题>" "<正文>"`) in exactly TWO
  cases, and no others:
  1. The ENTIRE task is finished — verified, analysis written — and is now
     waiting for the user to review. Not "the run finished, analysis pending";
     verified means the main session has confirmed the results are real before
     pushing (13:57 incident: a script pushed 「全部完成」 while one whole arm
     had silently produced zero data).
  2. Work is blocked and cannot proceed without the user (a decision only they
     can make, or something only they can physically do).
- Everything else is silent: phase/milestone completions inside a larger task,
  minor hiccups and recoveries the main session handles itself, watchdog or
  watch-loop alarms (they wake the MAIN SESSION, which intervenes; push only if
  the intervention then hits case 2), and restart-duplicated notifications.
- Scripts must not embed mid-run notify calls. The completion push comes from
  the main session, once, after verification — never from a phase boundary
  inside a queue script.
- The user's phone is permanently on silent; push is the only channel that
  reaches them away from the terminal. That is precisely why it must stay
  low-noise — a push per milestone trains the user to ignore pushes.
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

### Repository hygiene
- `git add` with explicit paths only, never `git add -A` / `git add .`. Commit
  and push only when the user asks; a reviewed diff is not an authorization to
  commit it, and a commit is not an authorization to push it.
- Every commit that contains code runs the full `./devctl.sh check` through
  the pre-commit hook, so reviewed groups are combined into one commit with a
  sectioned message rather than committed one topic at a time.
- Persist session artifacts into the repo the same day: harness and analysis
  scripts to `benchmarks/`, decisions and residual lists to the plan file under
  `docs/plans/`. The scratchpad is session-scoped and gets wiped.

## Compact instructions
When compacting, keep: the current phase and slice with its acceptance
criteria; the state of uncommitted work (which files, verified or not);
background tasks and agents still running, with what each was asked; the
user's standing authorizations and their most recent instructions, verbatim;
open decisions waiting on the user; every constraint the user stated in this
session.
Drop: command output, diffs, file contents, agent reports already recorded in
the plan file or a commit message. Point to the file that holds a fact instead
of restating it.
