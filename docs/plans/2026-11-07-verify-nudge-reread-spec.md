# 写后回读冗余修复规格（T1-4 写后验证门控 + 系统提示措辞）

日期：2026-11-07。背景：P2 批 `benchmarks/data/context-knee-20261107-p2/`（ANALYSIS.md §1.9、
item004_008_evidence.md）里 lc-hard-004 / lc-hard-008 在编排器五臂几乎全败，判败主体是「步数超限」
（004 众数 13 / 预算 11；008 众数 12 / 预算 10），其中每 run 4 步是写入回执已成功后逐文件 `read_file`
回读。直调参照（`context-knee-20261031/qwen-current/`）同题 8 / 7 步通过、无回读。

## 1. 根因（scout 2026-11-07 摸底，证据 `scratchpad/scout_sequences.md`；主会话逐行核对）

12 个 L0 run（两题 × none 臂 × 6 rep）的回读诱因：

| 诱因 | run 数 | 证据 |
|---|---|---|
| 编排器 T1-4 写后验证门控的推回文本逐字要求「re-read what you changed」 | 7/12 | `backend/framework/core/orchestrator.py:1762-1777`；回读前思考原文 "The guardrail is asking me to verify… Let me re-read the file and confirm"；同一 run 更早曾判断 "The tool results already confirm success" 但被门控推翻 |
| 模型自建计划含 "Verify final contents" 一步 | 4/12 | 系统提示 `backend/framework/memory/session.py:134` "After editing, verify: re-read the changed section…"；直调用的极简提示（`backend/benchmarks/model_eval/executor.py:28`）无此句，从不回读 |
| 模型自发（数 count 前读文件） | 1/12 | 008 r4，对计数任务有一定道理，不动 |

排除项：
- 回执信息量不是原因。write/append 两种模式走同一 mock env，回执逐字相同
  （`backend/benchmarks/model_eval/mock_envs.py:81,88`）；生产 `edit_file` 回执更详细
  （`backend/framework/mcp/tools/file_edit.py:294` "OK: replaced 1 occurrence in … (N chars -> M chars, file size ±d)"、
  `:358` "OK: wrote N chars to …"）；观察截断阈值 4000 字远未触及。
- 题面不要求回读：004（`backend/benchmarks/model_eval/items/longchain/hard.jsonl:8`）只要求 EACCES 后
  「leave it exactly as it is」并写 pending-changes.md；008 只要求追加 + 写计数。
- 计划全部完成提醒 `orchestrator.py:2158` 'confirm the key deliverables were truly verified ("executed" is
  not "verified")' 是第二层强化（004 r0 收到），同向修。

另：每 run 第 1 步都是一次失败的 `run_shell_command`（exit 127，沙箱无 shell），由系统提示
`session.py:125` "prefer the terminal" 诱导；本规格不动它（去掉 4 次回读后 9 ≤ 11、8 ≤ 10 已过；
shell 那一步是 harness 沙箱形态问题，另案）。

## 2. 设计（最小改动；门控保留其本职——代码修复「已执行 ≠ 已验证」）

T1-4 的本职是抓「弱模型改一行代码就宣布修好、从不运行」。对纯内容文件（.md/.txt/.log 等）
不存在可运行的「真检查」，成功回执本身就是验证；逐文件回读只是把回执里已有的事实再读一遍。
故：

- **D1 门控武装判据收窄**（`orchestrator.py` 约 1627-1633 的扫描循环）：
  - 只有**成功**（`not _obs_failed(r)`）且路径扩展名**不在** `_RECEIPT_IS_VERIFICATION_EXTS`
    的 `edit_file` / `write_file` 调用才置 `unverified_write = True`。
  - `_RECEIPT_IS_VERIFICATION_EXTS = {".md", ".txt", ".log", ".csv", ".tsv", ".rst"}`（模块级常量，
    紧挨 `_FILE_WRITE_TOOLS`，注释说明：这些扩展名没有可运行的检查，成功回执即验证）。
    扩展名取 `os.path.splitext(str(args.get("path") or ""))[1].lower()`；无扩展名 → 视为代码（仍武装）。
  - 对这类「中性」写调用（纯内容写、失败写）：既不置 True 也不清 False（失败写什么都没改，
    由 D2 失败吞没门控负责）。注意 004 里 EACCES 的 edit 写的是 .md，按扩展名本来就中性，
    004 不是「失败写中性」的证据（一审 m5 纠正）；那条语义由 lifecycles 单测钉住。
  - 其余非写调用照旧清标记。
  - 判据抽成 `_arms_verify_gate(call, obs)` 单一入口，主循环扫描与审批续跑路径（`resume_after_approval`
    的 `wrote_unverified`）共用；此前续跑路径是独立的一份判据（implementer 发现）。
  - `.txt` 例外（一审 m4）：`requirements*.txt` / `constraints*.txt` / `CMakeLists.txt` 是依赖 / 构建清单，
    改了有真检查可跑，仍按代码武装（`_CODE_LIKE_TXT`）。
  - 口径键（一审 m7，沿 stub_clue / mock_merge_rule 惯例）：`orchestrator.VERIFY_GATE_RULE = "prose-neutral-v1"`
    记进 knee harness 每行的 `verify_gate_rule` 并恒比（`HARNESS_MODE_KEYS` / `_ALWAYS_COMPARED`），
    旧批（行缺键 = 任何写都武装）续跑 / 追加即拒。
- **D2 推回文本改写**（`orchestrator.py:1770-1777`）：去掉「re-read what you changed」，明说回执已
  确认内容落盘、回读证明不了更多，验证指的是**效果**（运行代码 / 测试 / 命令，追踪改动确实生效并
  解决用户**原始**问题）；没有可运行检查时直接作答并说明未验证项。建议文本：
  ```
  [System guardrail] You modified state this turn (write tool calls) and then wrapped up without any real check. The write receipts already confirm the file contents landed, so do NOT re-read the files you just wrote — that proves nothing more. Do NOT tell the user it is done/fixed yet. First verify the EFFECT: if a real check is feasible, run it (execute the code, run the test or command, trace the flow end-to-end to confirm the change is actually reached and takes effect) and check that it resolves the user's ORIGINAL problem (not just a plausible nearby issue). If verification shows the fix is wrong or incomplete, keep working. If no real check is feasible, answer now and say explicitly what you could not verify and what the user should test — do not claim success.
  ```
- **D3 系统提示改写**（`session.py:134`，"Code fixes & debugging" 段）：
  ```
  - After editing, verify the EFFECT, not the bytes: a successful edit_file / write_file receipt already confirms the content landed, so do not re-read a file just to check a write. Whenever feasible run a real check (execute the code, test, or command). Claim success only for what you actually verified; if nothing can be run, say so plainly.
  ```
  快照 `backend/tests/data/model_visible_text.snap` 按既定流程重生成（`UPDATE_MODEL_VISIBLE_SNAPSHOT=1`
  跑 `tests/test_model_visible_text.py::test_model_visible_text_snapshot` 必红 → 不带变量重跑收绿；
  快照文件绝不手改）。
- **D4 计划完成提醒改写**（`orchestrator.py:2158`）：
  ```
  Also confirm the key deliverables are truly verified — for code, by a real check that actually ran ("executed" is not "verified"); successful write receipts already prove file contents landed, so do not re-read files just to confirm writes.
  ```
- D2 文案顺序（一审 m8）：「没有可运行检查就直接作答」放在「run the real check」之前，减少无 shell
  沙箱里为凑「真检查」而乱跑 run_shell_command 的诱导；「trace the flow」改成「follow the call path from
  the entry point to your change」，与「别回读刚写的文件」不打架（n10）。
- 不动：mock env 回执格式（不是原因）、`VERIFY_NUDGE_MAX`、D2 失败吞没门控、题目、门槛。
- 已知分歧（一审 m6，记档不改）：knee harness 的 mock `write_file` / `append_file` 失败回 `{"error": …}`
  JSON（`benchmarks/context_knee/orch_tools.py:61-64` 经 EnvTool），`_obs_failed` 判不出失败，所以 harness
  里失败写**代码类**文件仍会武装门控，与生产（"System Error:" 前缀）不一致。004/008 的写都是纯内容文件，
  不受影响；要对齐得另案评估 D2 失败口径。

## 3. golden test（FakeEngine，CPU-only）

- `backend/tests/test_context_knee_orchestrator.py` 新增 `test_prose_writes_do_not_trigger_verify_nudge_reread`：
  lc-hard-004 形态的 item（vfs：policy/security.md 只读 + backup.md + access.md；task.user 原题文案；
  expect end_state + `max_steps: 11`），prod 前缀。脚本：list_files → read_file ×3 → edit_file security.md
  （EACCES）→ edit_file backup.md → edit_file access.md → write_file policy/pending-changes.md → 收尾文本 →
  **（仅门控仍触发时才会走到的分支）** read_file ×4 → 收尾文本。
  断言：`rec["guard_events"].get("verify_nudge")` 为空；`rec["score"]["steps"] == 8`、`steps_ok`、
  `final_text_ok`、`pass is True`；`rec["gens"]`（或等价字段）等于不含回读分支的生成次数。
  修复前（或 D1 被反转）该脚本会吃到推回、走回读分支，steps 变 12 → 红。
- `backend/tests/test_orchestrator_lifecycles.py`：
  - 现有 `test_verify_nudge_on_unverified_write`：路径改为 `/a.py`（代码文件仍武装），追加断言推回
    tail_note 含 "do NOT re-read" 且不含 "re-read what you changed"。
  - 新增 `test_verify_nudge_skipped_for_prose_write`：`write_file` 到 `notes.md` 后直接收尾 → 第一条
    收尾即被接受（engine.calls 长度 2、无 verify tail_note、response 等于第一条文本）。
  - 新增 `test_failed_write_does_not_arm_verify_nudge`：`write_file` 到 `x.py` 回 `System Error: …`
    后收尾 → 只吃 failure_swallow_nudge，不吃 verify_nudge（看 tail_notes 文本 / events）。
- 若 `_obs_failed` 对 harness 桥接的 EACCES 回执判不出失败，先在测试里核实回执实际文本再定断言，
  不要削弱 D1 的「失败写中性」语义。

## 4. 变异证明（守卫型改动；verifier 串行执行，清 `__pycache__`，逐字节恢复）

| 变异 | 应红的测试 |
|---|---|
| m1：D1 扩展名集合置空（所有成功写都武装） | knee `test_prose_writes_do_not_trigger_verify_nudge_reread`、lifecycles `test_verify_nudge_skipped_for_prose_write` |
| m2：D1 去掉 `not _obs_failed(r)`（失败写也武装） | `test_failed_write_does_not_arm_verify_nudge` |
| m3：D1 判据反转为「只有纯内容写才武装」 | `test_verify_nudge_on_unverified_write`（/a.py 不再推回） |
| m4：推回文本恢复 "re-read what you changed" | `test_verify_nudge_on_unverified_write` 的文本断言 |
| m5：中性写改成清标记（`unverified_write = _arms_verify_gate(c, r)`） | `test_prose_write_after_code_write_keeps_verify_nudge` |
| m6：续跑路径改回旧判据（`c.get("name") in _FILE_WRITE_TOOLS`） | `test_resumed_approved_prose_write_does_not_arm_verify_nudge` |
| M-L（`tests/run_retrieval_guard_mutations.py`，改到续跑调用点注入） | `test_resumed_c4_blocked_file_write_is_not_an_unverified_write` |
| m7：`verify_gate_rule` 从 `_ALWAYS_COMPARED` 去掉 | `test_resume_refuses_orchestrator_dir_with_other_mock_merge_rule[verify_gate_rule-…]` |
| m8：去掉 `_CODE_LIKE_TXT` 例外 | `test_dependency_manifest_txt_still_arms_verify_nudge` |

结果（2026-11-08 凌晨，verifier）：m1–m8 全红、恢复后 shasum 全 OK。M-L 首跑存活：`test_retrieval_evidence_guard.py` 的
`VERIFY_NOTE` 钉的是旧推回文案 "never re-checked the result"，D2 改写后断言空置（变异存活先疑 fixture，果然）；
改成新文案稳定前缀 "You modified state this turn" 后 M-L 被杀、harness 20/20。全量 4369 passed / 71 skipped。

## 5. 真模型复测

待下一租约：`ARMS=orch-turn-none,orch-turn-full LEVEL_REPS=0=6 KNEE_SEED=1107`，看 004 / 008 L0
步数众数是否降到 ≤ 9 / ≤ 8、none 臂 L0 是否 ≥ 70%。
