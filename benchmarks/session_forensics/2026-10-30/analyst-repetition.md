# analyst-repetition evidence (tree @1ca9254, 2026-10-30 20:05:09)
Script: /private/tmp/claude-501/-Volumes-enkin-project-Agent/5f76a36a-f6d1-43b7-8719-ce9002957a9e/scratchpad/scripts/repetition_window.py (python3 -I). Grep dump: /private/tmp/claude-501/-Volumes-enkin-project-Agent/5f76a36a-f6d1-43b7-8719-ce9002957a9e/scratchpad/rep_grep.txt (476 hits, keyword grep over backend/framework + inference, tests excluded).

## R1 repetition_window.py output (web_research step lines omitted here; full in /private/tmp/claude-501/-Volumes-enkin-project-Agent/5f76a36a-f6d1-43b7-8719-ce9002957a9e/scratchpad/repetition_window.out)
```
J last tool_result seq=2336 17:00:53.377 name=read_ui_tree
J turn_ended seq=2857 17:01:22.952 status=cancelled
J window seconds=29.575 unpersisted_seqs=520
H status=cancelled last_gen prompt_tokens=24981 generation_tokens=1778 tps=62.9 reused=24800
H implied_decode_seconds=28.27  (window minus decode = 1.31s for prefill+cancel latency)
H max_tokens=4096 thinking_budget=2048 ESCALATED_MAX_TOKENS=12288
H gen/max_tokens=0.434 gen/thinking_budget=0.868 truncated_rule(gen>=max)=False
H turn gen tokens=8421 generations(non-cancelled)=6 minus_last=6643 breakdown_sum=6643 breakdown={'thinking': 0, 'visible': 6200, 'tool_json': 390, 'marker_overhead': 53}
LOG 5184: INFO:     100.95.73.50:0 - "POST /api/v1/chat/app-9daf9c9fee44/turns HTTP/1.1" 202 Accepted
LOG 5185: [Engine] 约束采样引擎就绪（llguidance）
LOG 5186: [Engine] 约束采样构建失败，永久禁用（纠错闭环兜底）: NotImplementedError: qwen35 方言使用原生 XML 调用体（<function=…><parameter=…>），按设计不启用 约束采样——engine 将 fail-open 到自由采样 + �
LOG 5258: [Engine] 前缀命中 14400/13557 tokens，gen 63.7 tps
LOG 5260: [Orchestrator] 执行工具: web_research
LOG 5261: [Engine] 前缀命中 0/1358 tokens，gen 135.7 tps  <sub:web_research>
LOG 5283: [Engine] 前缀命中 0/3802 tokens，gen 120.7 tps  <sub:web_research>
LOG 5303: [Engine] 前缀命中 4000/8302 tokens，gen 99.8 tps  <sub:web_research>
LOG 5314: [Engine] 前缀命中 7200/13588 tokens，gen 89.1 tps  <sub:web_research>
LOG 5345: [Engine] 前缀命中 13600/17685 tokens，gen 71.5 tps  <sub:web_research>
LOG 5385: [Engine] 前缀命中 16800/19841 tokens，gen 77.6 tps  <sub:web_research>
LOG 5567: [Engine] 前缀命中 18400/23081 tokens，gen 58.5 tps
LOG 5574: [Engine] 前缀命中 17600/17581 tokens，gen 68.7 tps
LOG 5575: [Orchestrator] 执行工具: web_research
LOG 5583: [Engine] 前缀命中 0/1354 tokens，gen 127.0 tps  <sub:web_research>
LOG 5621: [Engine] 前缀命中 0/4193 tokens，gen 105.6 tps  <sub:web_research>
LOG 5649: [Engine] 前缀命中 3200/6981 tokens，gen 115.0 tps  <sub:web_research>
LOG 5655: [Engine] 前缀命中 5600/10350 tokens，gen 93.0 tps  <sub:web_research>
LOG 5659: [Engine] 前缀命中 10400/13487 tokens，gen 88.6 tps  <sub:web_research>
LOG 5683: [Engine] 前缀命中 13600/15885 tokens，gen 89.4 tps  <sub:web_research>
LOG 5746: [Engine] 前缀命中 14400/17068 tokens，gen 70.1 tps
LOG 5755: [Engine] 前缀命中 19200/20063 tokens，gen 56.5 tps
LOG 5756: [Orchestrator] 执行工具: file_extract
LOG 5886: [Engine] 前缀命中 22400/20365 tokens，gen 55.8 tps
LOG 5887: [Orchestrator] transition → failure_swallow_nudge
LOG 5907: [Engine] 前缀命中 23200/24072 tokens，gen 52.2 tps
LOG 5908: [Orchestrator] 执行工具: file_extract
LOG 5943: [Engine] 前缀命中 24800/24683 tokens，gen 46.9 tps
LOG 5944: [Orchestrator] 执行工具: read_ui_tree
LOG 5995: INFO:     100.95.73.50:0 - "POST /api/v1/chat/app-9daf9c9fee44/task/cancel HTTP/1.1" 200 OK
LOG 5996: [Engine] 前缀命中 24800/24981 tokens，gen 62.9 tps
LOG marker counts in range: {'transition →': 1, 'runaway': 0, '[Constrained]': 0, 'max_tokens_escalate': 0, 'max_tokens_recovery': 0, 'duplicate': 0}
LOG last Engine line in range: reused=24800 prompt=24981 tps=62.9 | matches history last gen: True
MAIN-ish gens (line, prompt, tps): [(5258, 13557, 63.7), (5567, 23081, 58.5), (5574, 17581, 68.7), (5746, 17068, 70.1), (5755, 20063, 56.5), (5886, 20365, 55.8), (5907, 24072, 52.2), (5943, 24683, 46.9), (5996, 24981, 62.9)]
last tps=62.9 vs mean tps of prior gens with prompt>20000 (5): 54.0 ratio=1.17
worst-case tokens of a non-tool degenerate loop before acceptance: 4096 + 12288*(1+3) = 53248 ; at 62.9 tps = 14.1 min
remaining to thinking-budget force-close: 270 tok = 4.3s ; remaining to max_tokens: 2318 tok = 36.9s
```

## R2 guard inventory (path:line, tree @1ca9254)
| guard | measures | threshold | action | channel | when |
|---|---|---|---|---|---|
| repetition_penalty | logit bias on tokens seen | 1.1, ctx 50 (engine.py:426-432, 720); vLLM window = full context (cuda/backend.py:26, 491-492); MLX ctx 50 (mlx/backend.py:554-555) | soft sampling bias, never stops | all tokens | every decode step, server side |
| thinking budget | tokens in opening thinking segment | features.thinking_budget=2048 (engine.py:433-445); cuda two-phase stop=</think>, max_tokens=budget, forced "\n</think>" (cuda/backend.py:588-640); MLX ThinkingBudgetCriteria (gemma4_generate.py:1162) | force-closes thinking, generation CONTINUES into content | thinking only | at 2048 thinking tokens |
| max_tokens | gen tokens | 4096; truncated := gen>=max (engine.py:577) | post-gen: escalate to 12288 + up to 3 "continue" rounds (orchestrator.py:261-262, 918-934) | content/thinking | only after the generation ends |
| runaway constrained call | truncated AND constrained_steps>=512 (orchestrator.py:886-916) | max_parse_retries=3 (408) | discard + nudge, abort at 3 | tool args only | post-gen; constrained_steps needs the guard, which is disabled for qwen35 (backend.log:5186) -> dead on this deployment |
| parse-loop breaker | parse_errors per bad call / total (orchestrator.py:1115-1133, msg :62) | >=3 same or >=5 total | end turn status=warning | tool calls | post-gen |
| duplicate call | same name+args fingerprint (orchestrator.py:1671-1686) | 1 repeat | block call with message | tool calls | pre-exec |
| failure_swallow_nudge | wrap-up after unresolved failed call (orchestrator.py:1232-1244) | failure_nudges<1 (667) | push back once | final text | post-gen wrap-up |
| iteration fuses | max_total_iterations 40 (391, 784), max_sub_generations 80 (394, 785) | | end turn | turn | between gens |
| stream read timeout | silence between SSE chunks (remote/backend.py:37, 361) | 300 s | error | transport | only on silence |
| NOT guards | gemma4_generate.py:162-328 n-gram index = speculative drafter (MLX); gemma4/prompt_builder.py:73,360 = prompt shaping notes | | | | |
None in stream_filter.py (no repetition/loop/stall logic), turn_runner.py (hits = submission dedup only), inference/server.py, backends/cuda, backends/remote, dialects/qwen35. No n-gram / identical-line / digit-run detector anywhere on the streaming path.

## R3 last generation (from R1)
- backend.log:5943 gen -> 5944 read_ui_tree; journal seq 2336 17:00:53.377 tool_result; backend.log:5995 POST task/cancel; 5996 "[Engine] 前缀命中 24800/24981 tokens，gen 62.9 tps" (printed after the stream broke, engine.py:526-530); seq 2857 17:01:22.952 turn_ended cancelled.
- history.jsonl last-gen metrics = that line (prompt 24981, tps 62.9): generation_tokens 1778; 1778/62.9 = 28.27 s of decode in a 29.575 s window -> generating continuously until the cancel.
- 1778 < thinking_budget 2048 < max_tokens 4096: neither length limit reached; engine returns cancelled before truncation/breakdown (engine.py:544-545); orchestrator returns at 852-855; no "transition →" after 5887 (count in range = 1).
- Channel not recoverable: deltas are not persisted (PROTOCOL.md:104-105); backend.log prints only a per-gen summary; inference server is remote (backend.log:354 agent-compute.tail8d18fd.ts.net:14161; tailscale: linux, 35.184.40.174) -> no local inference log.
- thinking ON for every iteration: journal gen.enable_thinking=True, thinking_mode dynamic -> requested or iter==0 (orchestrator.py:518-524).

## R4 anomalies
- token_breakdown thinking=0 across 6 thinking-on generations: _token_breakdown regex is gemma4-shaped (<soc>thought ...<eoc>, engine.py:636-645); qwen35 prompt pre-opens <think> (dialects/qwen35/dialect.py:82-85) so generated text has no opening marker -> all thinking counted as "visible" (6200). Instrumentation defect; thinking/visible split invalid for qwen35.
- runaway-call guard structurally unreachable on qwen35 (constrained sampling disabled, 5186; 0 "[Constrained]" lines in range).
- Second file_extract used path "dev.injin.macagent" vs first "dev.inkin.macagent" (journal seq 2247 vs 714): duplicate-call guard correctly did not match; both paths are model-corrupted copies of dev.enkin.
- guardrail_events.jsonl (data root, 16410 B, mtime 17:10) not read: outside authorized paths; it would carry runaway_call / truncation_recovery / duplicate_call events.
