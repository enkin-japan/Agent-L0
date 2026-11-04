# context_knee — 有效上下文拐点测量（W0）

背景见 `docs/plans/2026-10-31-context-management-plan.md` §3 W0。复用 `backend/benchmarks/model_eval`（`run_item` + longchain 终态打分，源码不改），任务前注入可控长度的真实形态历史。
```
backend/.venv/bin/python benchmarks/context_knee/run_knee.py --dry-run --levels 0,4096 --reps 2 --limit 2
INFER_REMOTE_HOST=<算力端> INFER_API_KEY=<…> backend/.venv/bin/python benchmarks/context_knee/run_knee.py \
    --model RedHatAI/Qwen3.8-27B-INT4 --backend remote --tasks hard [--sampling repetition_penalty=1.0] \
    [--prefix mock|prod|catalog|routed]
backend/.venv/bin/python benchmarks/context_knee/summarize.py <out_dir> [<out_dir> ...] [--coherent-foreign 0.01 --coherent-mixed 0.05 --coherent-dup 0.3]
```
- 缺省 `--backend remote`：本机不装载模型（单 GPU 纪律）；只拉分词器（`resolve_tokenizer_only_path`，app.py remote 同款）+ 身份护栏。mlx 走 run_eval 同款全量解析、在本进程装载。`--dry-run` = FakeEngine。
- 产物落 `results/<模型>/<时间戳>/`（gitignored）。`--out` 已有 transcripts.jsonl 时拒绝，`--append` 追加；`--resume`（隐含 --append）跳过已成功的 (level, 题, rep)、error/timeout 行重跑，已有行的 model / 已发送采样 / seed / 注册表条目采样（`entry_sampling`，W-1 起引擎并入；旧行缺该键按 {} 算）与本次不一致即拒；单纯 `--append` 只比条目采样与前缀档（`prefix_mode`，见下节）。`--run-timeout`（缺省 600 s）超时：置 cancel → 排空推理执行器（等 min(60, timeout) 秒，等不到记 `drain_timeout: true` 并警告后续可能连环超时）→ 释放缓存 → 记 `{"error":"timeout"}` 行继续。summary.json 总会写出；任何率之前先去重：同 (level, 题, rep) 取最新的非 error 行（不按 stamp 拆 Pass^2）。
- `--sampling k=v,…`：映射键（见 run_knee.SAMPLING_KEYS）记为 `sampling_overrides.sent`——**已发送、未回执核验**（服务端不回显 SamplingParams），表里标 `card(sent …)`；其余键警告后不发送、记入 `ignored`。覆盖两层：引擎入口同名形参 + 后端 `stream_generate` 的 sampling 字典（引擎按注册表条目 `sampling` 解析，W-1；覆盖层仍优先）。SIGINT/SIGTERM：置 cancel → 排空 → 写 summary → 退出码 128+signum，12 s 看门狗兜底，收尾期二次信号立即硬退。远端无采样 seed：同种子只对填充成立。

## 前缀档 `--prefix`（W-2「量法」）
方案 §W-2：拐点曲线原是 harness 短前缀口径（executor 极简 system + 题目 mock 工具，base ≈0.3–0.6K tok），生产前缀是 13.6K（full）/ 5.3K（catalog）。四档在同一填充流上对比，装配在 `prefix.py`：
- `mock`（缺省）：W-2 前口径，逐字节不变（system、tools、run_item / 引擎入参都不改；有测试钉住）。
- `prod`：system = `framework/memory/session.py` 的 `DEFAULT_SYSTEM_PROMPT`；工具 = 生产确定性工具全集完整 schema + full 档元工具（`update_plan` / `complete_step` / `remember`）+ 题目 mock 工具；`grammar_tools` 与 prompt 面同集合。
- `catalog`：system 同 prod；元工具（多 `get_tool_schema`）与 `agent.tool_routing.core` 完整，其余生产工具只留目录行（`tool_disclosure.disclose`，只有 name/description 两键），其后 mock 工具完整；`grammar_tools` = 完整 schema 全集（与生产一致：语法面恒用完整）。
- `routed`：catalog 之上，按原题面 `task.user`（不含回忆句，跨 level/rep 不变）经 `tool_disclosure.route_tools` 选 `agent.tool_routing.top_k` 件，选中者完整 schema。每题一次、题内冻结。

细则：
- 本节说的是**直调模式**（缺省）；编排器模式的前缀语义见下文「编排器模式」。
- 生产工具集与 `backend/scripts/measure_prefix.py` 同源（`tool_token_audit.collect_schemas`：密封构造、不探活 RAG、不含 `rag_admin`、skill_invoke 取生产形）；声明顺序与生产 `MCPRegistry` 一致（sorted 模块 × `inspect.getmembers` 类名序，skill_invoke 因 app.py 补注册排末尾；有测试钉住）；元工具是 orchestrator 的模块级常量（`Orchestrator._meta_schemas` 同一份对象）。不构造 Orchestrator / MCPRegistry，不起服务。
- 撞名：题目 mock 工具优先（它承载评分），同名生产工具剔除，记 `prefix_shadowed`（vfs/office 题的 `write_file`）；被剔的 core 名记 `prefix_core_skipped`。
- **生产工具只声明、不执行**：模型调用它们（含元工具、`get_tool_schema`）时走 executor 既有的未知工具回执（mock env 回 `Unknown tool: <名>`，计入 env_errors），不新造执行路径。所以直调模式的非 mock 档测的是「大前缀下还能不能做对题」，不是生产编排的行为（生产编排见「编排器模式」）。
- 填充流按题目 mock 工具取名，不随档变：非 mock 档的伪轮次是同一 (seed, rep, 题) mock 档的前缀，只因前缀更大而更少。level 仍是 prompt 总量目标——低于前缀 base 的档（prod 下 4096/8192，fake 计数下连 16384）只有最少 3 个伪轮次、打 `fill.warning`，填充里放不下事实卡时记 error 行；非 mock 档请选高于 `prefix_base_tokens` 的档。
- 披露模式由 `--prefix` 显式决定，**不读**注册表条目的 `tool_schema_mode`（D29）：同一模型可分别量 full 与 catalog（D29 的选择正需要这组对比）。`prefix_base_tokens` 是整段前缀（含包裹与教学文本）的数，与 D28 门（逐件声明之和 12,300 线、两家族同口径）不是同一口径，不可直接对比。
- `summarize.py` 不按 `prefix_mode` 分组：不同档放不同 `--out` 目录，并排给 summarize 比较（同目录混档被续跑校验拒绝）。

## 编排器模式 `--orchestrator`（W-K）
直调模式（缺省）不经编排层：executor `run_item` 直调 `engine.generate_once`，W1 护栏（量测分级、钳制、ceiling 终止）、W2 步骤边界压缩、轮首机械压缩与轮统计都不在路径上。`--orchestrator` 让它们在路径上：每 run 新建 `MCPRegistry` + `Orchestrator`（agent 段 = `config.yaml` 的 `agent` 段深拷贝后按开关覆盖，文件不改）+ `ChatSession`，填充铺好后直调 `orch.run_turn`（不走 TurnRunner、不落 history.jsonl，harness 自记录），再按 history 映射成 executor 轨迹形态交给 longchain 评分器（评分器不改）。
```
backend/.venv/bin/python benchmarks/context_knee/run_knee.py --dry-run --orchestrator --levels 0,16384 --reps 1 --limit 2
... --orchestrator [--fill-placement history|turn] [--guard on|off] [--step-compaction on|off] \
    [--turn-compaction on|off] [--goal-verify on|off] [--prefix mock|prod|catalog]
```
- 开关（只在 `--orchestrator` 下有效，直调模式给了即 argparse 报错）：`--fill-placement`（**缺省 turn**）；`--guard`（缺省 on）→ `agent.context_guard.enabled`；`--step-compaction`（on）→ `agent.step_compaction.enabled`；`--turn-compaction`（on；turn 放置强制 off）→ `run_turn(compact_history=)`；`--goal-verify`（off，省一次生成）→ `agent.goal_verify`。`max_total_iterations` 按题覆盖成 `task.max_turns`（缺省同 executor：env 题 8），记入行 `max_total_iterations`；**元工具调用与编排层推回（提前收尾 / 验证 / 零证据 / 复读等 nudge）同计这个熔断**，同一上限下编排器模式留给 env 工具的回合比直调模式少。guard off 时编排层不量、不分级，W2 起压门槛（档位）恒不满足——guard off ⇒ W2 实际也不压。
- **§4 对照组：各臂固定同一放置、只切开关**（不跨放置比：turn 放置的 `in_turn` 填充与 history 放置的 `turns` 填充同一 level 下配对数差 10–20%，测量形态不同）：
  - turn 放置（`--fill-placement turn`）三臂：无管理 = `--guard off --step-compaction off` → +W1 = `--guard on --step-compaction off` → +W1+W2 = `--guard on --step-compaction on`（turn 放置下轮首压缩恒 off）。
  - history 放置单列为「现状参照」：`--fill-placement history --turn-compaction on --guard on`（轮首压缩 + W1；W2 碰不到历史轮）。
- 放置 `--fill-placement`：
  - `history`：填充插在题面 user 之前（system 之后），属**历史轮**；`run_turn` 自己把本轮起点置在 user 之后。碰得到它的：轮首机械压缩（`observation_budget_chars` 超了即驱逐保护窗外的旧观察）与 W1 ceiling 应急压缩（**范围是整个 history**）；W2 步骤边界压缩**碰不到**（W2 只压 [本轮起点, 步骤锚)）。
  - `turn`（缺省）：填充插在 user 之后，`turn_history_start_idx = 2`，以 `run_turn(keep_recent_calls=True, compact_history=False)` 进入——借审批续跑语义沿用预置锚点（新用户轮的轮首重置整段跳过），其余轮字段（步骤锚 None、峰值 / 档位 / 计数 0）是新建 `ChatSession` 的构造缺省（有测试钉住）。填充属**本轮**：W2 与 ceiling 都碰得到；轮首压缩强制 off（`turn_compaction` 记 off）。
  - **turn 放置下首次 hard 不压填充**：hard 钩子范围是 [本轮起点, 步骤锚) 且要求锚 > 起点，而预置会话的步骤锚是 None（首个 complete_step 落地才置位）——在首个 complete_step 之前 W2 只剩 complete_step 一路，hard 档到了也只发尾注。解读 +W2 臂时须区分「W2 没压」是没走到 complete_step 还是门槛没到。
  - 云端升级判定（轮首 `_upgrade_at_turn_start`）只在 `_fresh_turn = compact_history and not keep_recent_calls` 时跑：turn 放置恒跳过；history 放置 + `--turn-compaction off` 同样跳过；只有 history 放置 + 轮首压缩 on 才判。
  - 填充形态 `fill_shape`：history 放置 = 完整伪轮次（`turns`，与直调模式同）；turn 放置 = 轮内形态（`in_turn`）：只留 assistant(tool_calls) + tool 观察配对，无伪 user、无纯文本收尾，末条是 tool——模型接着看到的是「本轮我已做过的工具工作」。按 level 重新凑数：逐对代价按 [system, user, *配对]、本轮起点 2 计；凑完量整块，缺口超过一对平均代价就把缺口加进目标重凑（逐对单独渲染各付一次包裹开销，和式系统性偏离整块），残差记 `fill.fill_est_delta`（level − 整块估算）。伪轮次流与卡值同源。残留：last_k 回放下逐对代价按「思考被丢」的中段形态计，整块里最近 K 对的思考其实保留，K 很大时会多铺约 K 对思考的代价（缺省 K=2 可忽略）。
- 引擎代理 `OrchKneeEngine`：不注入填充（护栏的量测在 generate_once 之前）、不改写 tools（声明由编排器组）；`--sampling` 的同名形参经 run_turn 的 gen_kwargs 进编排层（max_tokens 先受 `generation_reserve` 钳制）；其余属性透传内层引擎（`measure_prompt` 只在内层有时才有）。主会话由 harness 预置，代理**不改写**它：收尾目标校验等子生成（EphemeralSession）也经代理，记入 `audit_gens` / `audit_generations`，不进 `gens`、不量测；release / full_renders / 信号取消都对主会话。gens 记账同直调，`measure_tokens` 取护栏刚量的那次（`measure_source: guard`），护栏没量时代理自己量（`proxy`）。
- 工具（`orch_tools.py`）：注册表的 tools_dir 是产物目录下的空目录 `tools-empty/`（非空即拒），**不注册任何生产工具实现**。mock env 每个工具一个 `EnvTool`（执行 = `env.handle` → JSON；`list_files`/`read_file`/`todo_list` 记 read、可并发、只读，其余记 write、串行、写屏障——与生产同名工具的分类一致，否则「零变更却声称已做」守门会对每个「已写入 …」收尾误推；分类记入行 `tool_effects`）；`--prefix prod|catalog` 另为每个生产工具注册一个**可用桩** `orch_stubs.UsableStub`（W-K2，见下表）。**spill 回读** `SpillReadTool`（name = `file_extract`，只读可并发）：W2 / ceiling / 截断的桩给的句柄是 `file_extract(path="spill:<stem>")`，path 以 `spill:` 开头时按 `framework/core/spill.resolve_spill_ref` 读本会话 spill 目录（落盘名正则 + realpath 限本会话、穿越拒），返回原文（可选 start/end 行区间）；其他 path 就是 file_extract 桩（下表 vfs 类；mock 档同样生效）。prod 档它顶替 file_extract 桩（声明取生产那份），mock 档以最小声明排在 env 工具之后（mock 档前缀因此比直调多一件声明）；成功回读计 `spill_readbacks`。元工具（update_plan / complete_step / remember / get_tool_schema）由编排器自带。**mock 档的极简系统提示不教 update_plan / complete_step**：+W2 臂是否有效看 `meta_calls`（没有 complete_step = W2 无从触发）；W2 验收建议 `--prefix prod`（生产系统提示）。披露（full / catalog + 任务期路由）由编排器自己做，生效值记 meta `tool_schema_mode_effective`：`prod` 按注册表条目 `tool_schema_mode`（> agent 段 > full），`catalog` 的注册表与 prod 相同、把条目层 `tool_schema_mode` 强制成 catalog（`run_knee.orch_model_options`），并把题目 mock 工具并入本 run 的 `agent.tool_routing.core`（`orch_tools.keep_mock_tools_full`，只改 run 内 cfg 副本，不进 `agent_cfg_fingerprint`）——元工具 + 生产 core + mock 工具完整（与直调 catalog 档的「mock 工具恒完整」同一对待），其余生产工具只留目录行，任务建立时按 top_k 路由。**gemma4 两条目在条目层已是 `tool_schema_mode: catalog`（config.yaml 模型注册表），它们的编排器 `prod` 本就按 catalog 披露（且 core 外的 mock 工具只剩目录行），编排器 prod / catalog 两臂对这两条目基本重合；Qwen 条目没设该键 = full，两臂才是 full 对 catalog。**`--prefix routed` 是直调模式的手工路由形态，编排器模式下报错。审批：进程内策略置 read/write = auto（不读数据根的 `approval_policy.json`），批次结束（含异常退出）还原。
- 生产工具可用桩（W-K2，`orch_stubs.py`；声明照生产完整 schema，`effect` / 可并发 / 只读取生产工具类属性，审批属性不照搬——harness 审批 = auto，钉死审批会让 run 挂起）：

  | 类 | 工具 | 行为 | 与生产的差异 |
  |---|---|---|---|
  | vfs 桥接（题目 env 有 vfs 工具集时；否则落离线类） | list_dir、find_files、edit_file、copy_file、move_file、trash_file、file_extract（非 spill）、run_shell_command | 一切读（cat / grep / head / tail / wc / find、file_extract、edit_file 与 copy 的源读）取 env 快照，不消耗题目为模型自己的 `read_file` 设计的故障配额；只有写 / 删经 `env.handle`（只读路径、写侧故障配额照常生效）；输出形照生产（list_dir 的条目行、find_files 的「N matches」头、file_extract 的行号前缀、shell 的 `exit_code:` 首行） | 路径是 vfs 相对键（前导 `~/` `./` `/` 剥掉，find 输出不带 `./`）；mtime 恒 2026-01-01；trash = 从 vfs 删除；shell 是只读仿真：`ls` / `cat` / `grep [-n] [-i] [-r]` / `find [dir] -name` / `head`·`tail -n N` / `wc -l` / `echo` / `pwd`，管道 / 重定向 / 串接与其他命令回 `exit_code: 127` + 说明（编排层判失败，同生产非零码） |
  | 检索 | rag_search、rag_get_context | rag_search 恒回生产空库原句 `No results for '<q>' in category '<c>': the target store is empty or unavailable …`，rag_get_context 回 `No chunks found for that path.`——`retrieval_evidence` 都判 `empty`，不挂待决标记、不拦交付 | 知识库恒空 |
  | 计算 / 常态 | run_math、get_current_time、system_status、calendar_list、schedule_list、deck_status、list_applications、read_ui_tree、clipboard_read | run_math 真算（ast 白名单：数字、`+ - * / // % **`、括号、sqrt/abs/round/min/max/floor/ceil/sum、赋值、print；REPL 式回显末行表达式）；时间恒 `2026-01-01 Thursday 09:00:00 JST (UTC+0900)`；其余回生产同形的空 / 常态结果 | run_math 无 SymPy / numpy（回 `Math sandbox error …`）；无日程、无桌面应用 |
  | 离线不可用 | 其余（web_*、cloud_consult、deck_* 写类、calendar_add、reminder_add、schedule_* 写类、memory_write、notify_user、clipboard_write、open_path、run_applescript、UI 动作、get_location、register_*、skill_invoke …） | 正常文本：本离线沙箱不可用、什么都没做，有 vfs 时指向文件工具 | 不伪造副作用成功；一律 `is_read_only=True`（effect 仍照生产）——「什么都没做」不算成功变更，D2「零变更却声称已做」守门照常推回 |

  失败回执分两档：**写类**（edit_file / copy_file / move_file / trash_file：old_string 不中或不唯一、源不存在、目标已存在…）与**缺必填参数**（措辞照 registry 的 execute() TypeError 回执）用生产前缀 `System Error: `，编排层判失败（D2 失败吞没推回照常）；**读类**（list_dir / find_files / file_extract 找不到等）用同一措辞的 `Error: `，编排层不判失败；shell 失败是 `exit_code: N`。评分器都不计 env_errors（不是 JSON）。行字段（成功、timeout、异常行都有）：`stub_served`（`{生产工具名: 本 run 桩执行次数}`，含 vfs 桥接与离线回执，不含 spill 回读）、`stub_failed`（桩自己发的 `System Error:` 数；按桩记下的回执原文逐条认领，**不计入** `system_errors`——后者仍只表示编排层回执与注册表未知名，与 11-02 分析口径一致）、`stub_unknown`（feed 里 `{"error": "Unknown tool: …"}` 回执数：**绊线**，应恒为 0——模型幻觉出的工具名走注册表的 `System Error: No tool named …`，计 `system_errors`；`analyze_orch.stub_calls` 仍是旧口径的 Unknown 回执计数）。meta `stub_impl = "usable-v1"`（W-K2）进续跑口径且恒比：W-K2 之前的编排器目录（缺键）与之续跑 / 追加一律拒。**可比性**：编排器 `mock` 档非 spill 的 `file_extract` 现在读 vfs（W-K2 前回 `Unknown tool: file_extract`），与 11-02 及更早的编排器 mock 臂数据不完全同口径。直调模式不受影响：生产工具仍只声明、回 mock env 的 `Unknown tool`。

  **D44 实参预检（2026-11-04 起）**：编排器模式下缺必填参数（缺席或值为 null）/ 未知参数先被生产注册表的通用声明核对（`MCPRegistry.validate_call`）打回，桩的 `execute` 不再被调用——回执是 `System Error: '<名>' was not executed: missing required argument(s): 'x'; unknown argument(s): 'y' (accepted: …).`，catalog 模式后接 `Full declaration of '<名>':` + 与 `get_tool_schema` 同一渲染的完整声明，full 模式后接一句 `Check the tool's declaration and call it again.`；它是编排层回执，计 `system_errors`、不计 `stub_failed`；编排层把它当失败的调用（失败吞没推回、升级计数照常），原样重发按 `Duplicate call blocked` 拦（上段桩自己的缺必填措辞只剩绕过预检直接 `execute_tool` 的路径会出现）。分析侧（`analyze_orch.py`）：`schema_fed` = 本 run 预检回执附了完整声明的次数（只认 `tool_error` 事件 `kind=precheck` 的 `schema_fed`；臂无事件流 = —）；catalog 模式下该工具完整声明已在前缀里（core / 路由件）就不附，给 full 模式尾句；`missing_arg_recovered` = 参数类（缺必填参数 / 未知参数）`System Error` 回执中、被之后同一工具的一次成功回执（不是 System Error / 非良性非零退出码 / `{"error": …}` / 去重或拒批拦截）收回的条数，按序先进先出、一次成功只收回一条，Q 节写作「（其中随后同工具成功 n；schema_fed n）」，CSV 同名列。catalog 对 full 的比较要两样一起看，再加首 prompt token（catalog 省的前缀 vs 预检多花的迭代）；Qwen 是否以 catalog 为缺省，取下一批的数据再定。同批 D43b：W2 步骤边界压缩不再驱逐 spill 回读（`file_extract(path="spill:…")` 的结果），首次免压的条数 > 0 即另发 `step_readback_exempt` 事件（`count` / `step_id` / `level` / `start` / `end`；同一条回读跨边界只计一次，全免的边界也有事件，`step_compaction` 事件不变），K 节列 `W2免压回读`、CSV 列 `w2_readback_exempt` 取其 count 之和。**可比性**：从 D44 这批起，编排器模式下桩不再默默接受未声明的实参键（b2 的桩照收），成功率 / 迭代数 / `system_errors` 与 b2 不能直接对比；D44 决策里「未知参数」一类要单列报告。
- 事件：批次装配最早处（`make_engine` 之前，远端装配期事件也落这里）把护栏事件流改指 `<out>/guardrail_events.jsonl`（缺省落 `data_dir()`，即 backend/ 或用户数据根——绝不写进去），批次结束（含异常）还原；每 run（含 timeout / 异常行）记事件文件行号区间 `guard_events_lines` 与按类型计数 `guard_events`（`context_soft` / `context_hard` / `context_guard_fold` / `context_guard_max_tokens_clamped` / `context_guard_unbound` / `step_compaction` / `turn_abort` / `goal_verify` …）。会话目录 `<out>/sessions/<L档-题-rep-随机>/`（W2 spill 文件在其 `spill/` 下，是证据，run 后保留）。
- 轨迹映射：本 run 自己追加的 history 段（不含填充）→ assistant 一条一个 turn（`tool_calls` 不含元工具，元工具按名计 `meta_calls`），tool 消息 → 该 turn 的 `feed[*].content` = 观察原文；"Duplicate call blocked" / "System Error" 开头的观察另计 `dup_blocked` / `system_errors`——它们不是 JSON，**不计 env_errors**（直调模式没有这两类回执：重复调用照常执行、未注册名走 mock env 的 `Unknown tool` JSON 计 env_errors），两种模式的 env_errors 因此不完全同口径，比较时把这两列一起看。`aborted_reason`：本 run 的 `turn_abort` 事件 reason（ceiling = `context_guard`；回合熔断 = executor 同一句「回合上限 N 耗尽仍在发调用」），cancelled / pending_approval / suspended 记状态；无 turn_abort 的 warning（D2 推回后仍坚持的降级收尾）不算 aborted，状态见 `turn_status`。ceiling 终止时 `turns[-1].text` 置空（护栏样板不是答案），run_turn 的答复原文另记 `turn_response`。
- **不可与直调模式混跑**：续跑 / 追加校验 meta 的 `mode`、五个开关 / 放置、`fill_shape`、覆盖后的生效值 `guard_thresholds {soft, hard, reserve}` / `step_compaction_cfg {trigger_level, cold_refill_trigger_level, keep_recent, digest_chars}`、`tool_schema_mode_effective`、`stub_impl`（W-K2，缺键也比）、`agent_cfg_fingerprint`（生效 agent 段——开关覆盖后、逐题回合覆盖前——`json.dumps(sort_keys)` 的 sha256 前 16 位），不同即拒；旧行缺 `mode` = 直调（只比 mode 与 fill_shape），编排器旧行缺的键不比——对照臂各放各的 `--out`。

## 思考回放口径 `--thinking-replay`（W3）
W3（D36，2026-11-01）起引擎按注册表条目的 `thinking_replay` 投影历史思考（缺省 `last_k`、K=2，Colab 两条目即此；本机 MLX 两条目 `tool_turns`），计数器 `PromptCounter` 与引擎同一投影。题面之前的填充属历史轮，填充里的思考会被丢掉，plan_run 为凑到 level 会多铺填充。10-30/31 的已有结果是**全部回放**口径，要同口径对比就传 `--thinking-replay all`。`--thinking-replay {all,tool_turns,last_k}` / `--thinking-replay-k N` 缺省都不给 = 沿用条目策略；只给一个时另一个取条目值，非法值启动即拒。实际生效的口径记入 meta。续跑 / 追加会校验这三个键里的口径（mode，last_k 时连 K）：旧行缺键视为 `all`，与本次不同即拒——不同口径请放不同 `--out` 目录。

## level 语义
`--levels` 是**首次生成的 prompt 总 token 目标**（system + 工具 + 填充 + 任务），缺省 `0,4096,8192,16384,32768,49152,57344`。
0 = 自然基线（无填充、无回忆句）；4096 = 带回忆句的最小填充、与高档同口径的基线；57344 = 顶档，给远端 max_model_len 65536 留生成余量。level 低于「基础 prompt + 3 个伪轮次」时打警告并记入 `fill.warning`。

计数按引擎同款组装渲染，再用引擎分词器计（fake 模式按 chars/3.5）；首次生成后用 `metrics.prompt_tokens` 回填。
填充是伪轮次流的前 n 轮：user 短问 → assistant 思考加一次调用 → tool 观察（`response` 键）→ 收尾。观察是 mock 环境同款 JSON 回执，读型观察长度 300–3000 字、对数均匀。
- 流由 (seed, rep, 题面指纹, 工具集) 决定：每题独立；同一题内，高档的伪轮次是低档的超集。
- 键名差异：填充用 orchestrator 的 `response` 键（compactor 只看这个键；qwen35 / gemma4 都能渲染）。执行器自己回喂的观察仍是 `content` 键，所以 compactor 看不见任务本身的观察。

**与 mock 环境的隔离**：伪历史的路径全部落在 `/srv/legacy-2025q3/…`，与 longchain 全部题面的路径和目录不相交（有测试钉住）。待办名带 `legacy-2025q3` 前缀，每条伪 user 消息都点明这是「上一个已归档、与当前无关的项目」。
残余：模型仍可能回读伪文件，mock 环境回 File not found，计入 env_errors。

## 事实卡（精确回忆探针）
- 每个 level>0 的填充埋 3 张卡，各是工具观察里的一行：halyard 配置路径、OPS- 工单号、`queue_depth_alert_threshold` 数值。
- 卡落在填充 token 轴的 10% / 50% / 90% 处；卡种按 (题序 + rep) % 3 在槽位间轮换（`cards[].kind`）；卡值在填充里查重，撞车就重抽（最多 20 次）。
- 任务 prompt 末尾追加一句，要求逐字写出三值；终态断言不变。
- 判命中前答复先 NFKC 归一、U+2010–2015 换成 "-"；值须整体出现（两侧不紧贴字母数字、后面不跟 ".数字"）；aborted run 不进 recall 分母。
- **回忆来源（D43 (a) 起必读）**：D43 (a) 之后驱逐桩带一行检索线索（`; mentions: …`，ID / 路径 / 全段唯一的 key = value），线索可以把卡值本身原样写在桩上——57K 档 recall 升高从此**不再证明模型会回读检索**。所以 analyze 表 B 在 recall 旁另列 `recall_src 桩/回读/原文`（末轮正文里出现的每个卡值归因：卡观察没被压 = 原文；压了且某次 `file_extract(path="spill:…")` 结果里有它 = 回读；没回读到但值在桩首部 200 字或线索里 = 桩；都不是 = `?n`，回读优先于桩；全部 run 合计、含 aborted）与 `回读调用(run)`（`spill:` 回读调用次数，括号里是有回读的 run 数）。读 recall 必须连同这两列：只涨「桩」不涨「回读」= 线索把值带出来了，检索本身没被测到。归因读 `<组目录>/sessions/<session_dir 末段>/` 的 spill/ 与 journal.jsonl；行 meta 的 `stub_clue`（线索规则版本，缺键 = D43 之前的批、桩只有首部）决定线索算不算进「桩上看得见」。口径细节见 `analyze.recall_source` docstring。

## 输出与口径
transcripts.jsonl 每 run 一行：
- `fill`：目标 / 估算 / 实际 token、fill_error、warning、cards、fingerprint；`score`：longchain 原字段 + recall_* + repetition_hits。`fill.prompt_tokens_measured`：首次生成前量法值（护栏 fold 前）；与 prompt_tokens_actual 不同时说明首生成前折过。
- `gens`：每次生成的 prompt_tokens、reused_prefix、prompt_tps、generation_tps、repetition_guard；`measure_tokens` / `measure_method`：该次生成前按同一份 prompt 入参调 `engine.measure_prompt` 的量值与量法（W1 护栏量法：`tokenizer` | `chars`），与同行实测 `prompt_tokens` 对照即量法偏差；内层引擎无此方法 = null，量法抛错另记 `measure_error`、不中止 run。旧行缺这两键。
- `full_renders`：本 run 会话的全量渲染次数（`session.turn_full_renders`，engine 每走一次全量路 +1；直调模式不经编排层、无轮首归零，executor 每 run 新建会话 = 整 run 口径；编排器模式同为整 run，每 run 新会话）。null = 会话没有该计数；旧行缺键。
- 上下文上限（meta，每行与 summary.json 都有）：`context_limit` = `engine.context_limit`（W1 护栏同一读法），`context_limit_source` = `health`（算力端 /health 报的 max_model_len）| `config` / `default`（remote 回落执行端配置 / 内置缺省；mlx 为 `inference.mlx.context_limit` 有无）| `fake`（干跑，无真上限，`context_limit` = null）| null（记不到）。旧行缺键。
- `peak_prompt_tokens`（W1）：本 run 各次生成实测 prompt_tokens 的最大值（gens 空或全缺 = null）；summarize / analyze 汇总为「峰值prompt中位」`peak_prompt_tokens_median`（W1 前的旧行缺键，不进中位数）。直调模式不经编排层，W1 护栏动作（钳制/压缩/终止）不在此计数；编排器模式也只取 gens 实测 prompt_tokens 的最大值，即实发峰值。
- `peak_prompt_measured`：编排器量到的峰值，含护栏 fold 前的量值；实发峰值看 peak_prompt_tokens。直调模式无此键。
- 其余：`final_text_quality`、`sampling_overrides`、`turns`。
- harness 口径（meta，每行与 summary.json 都有，W-K）：`mode`（`direct` | `orchestrator`；旧行缺键 = direct）、`fill_placement`、`guard`、`step_compaction`、`turn_compaction`（实际生效值）、`goal_verify`、`fill_shape`（`turns` | `in_turn`；直调 = turns）、`guard_thresholds`、`step_compaction_cfg`、`agent_cfg_fingerprint`、`orchestrator_cfg_source`（`config.yaml:agent` + 覆盖项）、`tool_schema_mode_effective`、`stub_impl`、`stub_clue`（D43 (a) 驱逐桩线索规则版本，如 `R2`；旧行缺键）；直调模式除 mode / fill_shape 外全为 null。
- 编排器模式行另有：`turn_status`（run_turn 的 status）、`turn_metrics`（run_turn 返回的 metrics 整个 dict：generations / peak_prompt_tokens / context_guard_level / step_compactions / compaction_freed_chars / full_renders / thinking_replay 等，后几项「非零 / 有值才出现」）、`guard_events` / `guard_events_lines`、`meta_calls`、`dup_blocked`、`system_errors`、`session_dir`、`turn_response`、`audit_generations` / `audit_gens`、`spill_readbacks`、`stub_served` / `stub_failed` / `stub_unknown`（W-K2）、`fill.fill_est_delta`（turn 放置）、`max_total_iterations`、`context_guard_bound`（引擎报得出上限才绑）、`tool_effects`（mock 工具的 read/write 分类）；`gens[*].measure_source`。summarize / analyze（表 B）末尾三列：W2 压缩次数中位、全量渲染次数中位（均取 `turn_metrics`，缺键按 0）、ceiling 终止计数（`aborted_reason == context_guard`）；没有编排器行的档显示 —。analyze 的 CSV 列不变。
- 思考回放（meta，每行与 summary.json 都有，W3）：`thinking_replay_mode` / `thinking_replay_k`（实际生效值）、`thinking_replay_source`（`cli` = 命令行覆盖，`entry` = 条目显式键，`default` = 条目没写、模块缺省 last_k/2）；W3 之前的旧行缺键 = 全部回放。
- 前缀（meta，每行与 summary.json 都有）：`prefix_mode`（W-2 前的旧行缺键 = mock）；`prefix_base_tokens` = 生产稳定前缀 token（system + 生产声明，未剔撞名、不含 mock 工具与路由件、`include_env=False`，与 `measure_prefix.py` 的 full / catalog(meta+core) 行同口径；mock 档 = 极简 system 无工具）。真后端用引擎分词器；fake 用 `backend/models/<repo 末段>` 离线分词器（`--model` 给了且目录在时），不可得记 null 并告警一次（mock + fake 不载分词器、不告警）。非 mock 档另有 `prefix_shadowed` / `prefix_core_skipped` / `prefix_n_tools`（按题：prompt 面件数、其中完整件数、语法面件数）；routed 档另有 `prefix_top_k`、`prefix_routed`（选中名单）。这四个按题的键在 meta / summary.json 是 `{题: 值}` 全批字典，transcript 每行只放本题的值（`run_knee.PER_ITEM_META_KEYS`）。

summarize 口径：
- **prefill 秒(未命中)** = 首次生成 prompt_tokens / prompt_tps，不按 reused_prefix 过滤：远端首生成报的 num_cached_tokens 实测不可信。对照列「后续 prompt_tps 中位」：真命中缓存时高一到两个数量级。
- **连贯率**：抓「漂进任务语言之外的文字系统」。coherent = 外文字母占比 `foreign_script_ratio` < 0.01（预期文字按题 lang：zh = 拉丁 + 汉字，en = 拉丁，ja 另加假名；数字、标点、符号不计）且 16-gram 重复占比 < 0.3，en 题另要求词内拉丁与 CJK 混写的 token 占比 `mixed_token_ratio` < 0.05（zh 题只记录）。summarize 按记录的 lang 从末轮正文现算；script_mix、longest_run 等旧指标照记，不进判据。局限：不含外文字的语义乱码抓不到。
