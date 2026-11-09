# 拐点 Colab D48 复测批（W1 落刀修订：hard 51,200 + fold minimal 对无管理，49K / 57K × 6 rep）：复算与结论（2026-11-09）

> 方案：`docs/plans/2026-10-31-context-management-plan.md` §2（验收三条；第二条 11-09 改防溢出口径）、§6 D48、§9 11-09 白天（起机单与判据）；harness 口径 `benchmarks/context_knee/README.md`。
> 原始数据：本目录 `orch-turn-none/`（guard off / step off）、`orch-turn-w1w2-rev/`（guard on + step on + `--guard-hard-tokens 51200` + `--guard-fold-mode minimal`），两臂 `--prefix prod`（生效 catalog）、同一 VM 顺序跑。档位 L0（2 rep，口径锚）/ 49152 / 57344（各 6 rep），hard 10 题，`KNEE_SEED=1107`（每 run 种子 = 1107×1000 + rep）。Qwen3.8-27B-INT4（Colab vLLM，MTP 关，current 采样）。两臂各 140 行，error 行 0、去重 0（A 节、证据 a），arms_status 两臂 ok，`DONE BATCH_RC=0 SKIPPED=0`。
> 时间线（`run_colab.log`，V5、N 节、证据 g）：08:31:04 首次 `START deploy`，08:32:42 state=stopped（未 READY），按 READY_RETRY 停机后 08:32:44 第二次 START；08:50:32 READY，冒烟 08:50→08:51 OK；none 08:51:32→12:15:45（204.2 min）、rev 12:15:45→15:27:57（192.2 min）；15:28:01 stop rc=0、DONE；首行 → DONE 417.0 min（6.95 h）。费用 602.08 → 555.93 = 46.15 单位（主会话记账；本目录无余额行，不能从产物复算）。
> 对照：`--p2 ../context-knee-20261108-w12eval/`（W 节，只有 none 同名对照；rev 无同名臂属预期，w12eval 的 w1w2 = 生产缺省 hard 39,322 + fold full，跨批对比在证据 e 节）；`@d44` = `../context-knee-20261105-d44/`；`@b2` = `../context-knee-20261102-b2/`；参照 `../context-knee-20261031/qwen-current/`。
> 脚本：`benchmarks/context_knee/analyze_p2.py`（本轮加 `--arm-b NAME`，只配 `--two-arm`，缺省 `orch-turn-w1w2`；w12eval 重跑 MD 与改前脚本除复跑行外逐字节相同、CSV 与档案逐字节相同，见 §2）；`d48_evidence.py`（本目录，写 `d48_evidence.md`：输入去重、冒烟漂移、L0 锚、§2 三条、零分布门槛、fold 机制、成本、口径）。证据脚本连跑两次输出逐字节相同。
> §1、§2 由 analyst 起草；§3 留主会话；括号里的字母 = 自动部分节，小写 a–g = `d48_evidence.md` 节；凡标「推断」的不是测出来的。

## 1. 结论
1. **输入与口径锚正常（证据 a、b、g，V1、W1）。** 两臂 140 对 (level,item,rep) fill.fingerprint / task_fp 全同，填充档 20 组 rep 间 fingerprint 全互异，种子 0 错。L0：none 13/20 = 65%、Pass^2 5/10 = 50%；rev 与 none **20/20 逐字相同**（gens prompt_tokens 序列也全同），rev L0 的 soft / hard / fold / step_compaction / turn_abort 事件全 0——L0 不触发任何护栏，符合设计。对 w12eval none L0（6 rep 73.3% / 60%；只取 rep 0–1 是 12/20、4/10）同水平。「error」字样行两臂各 2 行，都是同一 (L0 lc-hard-005 r1、L49152 lc-hard-008 r4) 的 type=error 工具调用（畸形调用回执），不是 run 级错误，计入分母。
2. **算力端漂移（推断）：同输入同种子跨 VM 第 3 轮分叉，同 VM 内可复现（a2、W1）。** w12eval 冒烟与本批冒烟（L0 lc-hard-001 r0，seed 1107000）非输出字段只差 guard_thresholds 多了 `fold_mode` 键（D48 归一，值 full，等价），gens 0–3 prompt_tokens 同，turn 3 thoughts 第 2 字符起分叉（与 w12eval 对 p2b 的形态一样）；本批冒烟与本批 none、rev 同键行逐字相同。none 跨批（w12eval → 本批）输入签名 140/140 相同，输出相同仅 1/20、1/60、0/60。
3. **同 VM 内共同随机数在 49K 成立（b、V1a、V1d）。** 49K 两臂首生成输入相同，首轮输出 60/60 逐字相同，**27/60 全程逐字相同**（rev 在这些 run 里没碰到 hard）；分叉的 33 对 rev 赢 3 / none 赢 1。57K 两臂输入因 rev 首生成前 fold 而不同，57/60 在 turn 0 分叉（rev 赢 12 / none 赢 14）。
4. **§2 第一条（成功率 / Pass^2 ≥ 无管理 90%）：合并点估满足；57K Pass^2 单档点估不满足；两档差都在噪声内；区间判不出非劣效（c1、d，V4、E）。** rev − none 按题配对：49K 成功率 45.0% → 48.3%，+3.3 [0.0,+8.3]，比 1.07 [1.00,1.19]；Pass^2 22.7% → 26.7%，+4.0 [0.0,+10.7]，比 1.18 [1.00,1.42]。57K 成功率 43.3% → 40.0%，−3.3 [−20.0,+13.3]，比 0.92 [0.62,1.41]；Pass^2 22.7% → 18.0%，−4.7 [−22.7,+12.0]，比 **0.79** [0.33,2.67]。两档合并：成功率 44.2% → 44.2%，0.0 [−7.5,+7.5]，比 1.00 [0.86,1.21]；Pass^2 22.7% → 22.3%，−0.3 [−7.0,+6.7]，比 0.99 [0.79,1.62]。§2 原文两项门槛都是 0.90（委托里的「≥ 0.75」在 §2 原文里没有，按 0.90 判）。判定标记：点估合并 ✅、57K Pass^2 单档 0.79 ✗、区间非劣效不可判。
5. **零分布门槛按同法重算（d）。** 本批 L0 每臂只 2 rep，m=2 / m=3 都拆不出零分布（拆分数 0，V2c 全表「超出支撑」）。本批唯一的同法零分布 = 填充档臂内 6 rep（none + rev 各 10 种 m=3 拆分）：49K 成功率 ≥ 23 / Pass^2 ≥ 23，57K ≥ 27 / ≥ 20，两档合并 ≥ 23 / ≥ 23（m=3，P ≤ 5%）。p2b none+full L0 复现委托门槛 23 / 27（脚本断言），w12eval none L0 6 rep 的 m=3 超出支撑。四个单档观察差 |3.3|、|4.0|、|3.3|、|4.7| 在三种零分布下都「噪声内」，尾概率 65–100%。**判读：49K / 57K 都回到与无管理同水平（噪声内）**；对比 w12eval 生产缺省 w1w2 的 49K −11.7 / −10.0、57K −6.7 / −12.7（跨批、不同 VM，none 自身跨批也差——49K −5.0 / −7.3、57K −1.7 / −3.3（W2）——只作方向参考）。
6. **§2 第二条（新口径：rev 每 run 实发峰值 ≤ 53,248 且 0 次 reason=context_guard（ceiling）终止）：✅（c2，J）。** rev 140/140 run 实发峰值 ≤ 53,248，最大 53,216（49K；57K 最大 48,770），> 53,248 的生成 0 次；turn_abort 两臂都只有 max_iterations / repetition_guard（rev 37 + 1），reason=context_guard（ceiling 终止）0 次；两臂 turn_abort 原因只有 max_iterations / repetition_guard。护栏量值 = 引擎回报（J：gens 1311 次差 0），所以「量到 ≥ ceiling 才折」直接保证发出去的 ≤ ceiling。none（对照，不受约束）83/140 run 超过 53,248，最大 63,360（< max_model_len 65,536，none 本批也没真溢出）。
7. **§2 第三条（总耗时增量 ≤ 25%）：✅（c3，V5）。** Σwall 49K none 96.0 → rev 97.0 min（+1.1%），57K 98.7 → 86.2（−12.6%），两档合并 194.6 → 183.2（−5.9%），含 L0 −5.6%；run_colab.log 臂墙钟 204.2 → 192.2 min（−5.9%）。每 run wall 中位两档几乎相同（100.1 / 99.7 s、86.3 / 87.1 s）。
8. **机制：hard 推后生效，minimal fold 只省了一部分（e、e2、e3，I、J、K）。**
   - 49K：context_hard 首生成前 **0/60**（w12eval 60/60），只在 30 run 的后续生成触发，prompt 51,279–53,120，全部 ≥ 51,200；ceiling fold 8 次（8 run，iteration > 1），全 minimal、passes 全 1，53,437 → 47,558（中位），8/8 折后 < hard，无 run 二次 fold、fold 后无生成再到 ceiling。hard 级 step_compaction（34 次，均 detail.level=hard、trigger=step，即步骤边界压缩；trigger=hard 的 W2 hard 钩子本批 0 次，w12eval 亦 0）一旦触发仍把 prompt 砍到约 27K：e3 记 36 次 / 31 run 出现 > 2,000 tok 的生成间下降，下降后中位 27,112（w12eval w1w2 49K 同口径 26,887）；按事件逐一核对，36 次里 8 次正是上面 ceiling fold 自身的 before→after（与 e2 范围一致），其余 28 次（含 5 次落在同一 fold run 内、fold 之后紧接的下一次下降）都是 hard 级 step_compaction 落刀。读法：hard 推到 51,200 让约一半 run 根本不进压缩路径，而进了的 run 压缩深度与 w12eval 相同。
   - 57K：首生成前 fold 仍是 **60/60**（填充 57,554 > ceiling，必然），但 minimal 把首生成 prompt 从 w12eval 的 27,158 提到 **41,104**（35,044–46,354）；passes 全 1、每 run 只 1 次 fold、compacted 中位 35 条。目标是刚好 < 51,200，实际停在 hard 下方中位 10,096 tok：实省 ÷ 缺口（before − hard + 1）中位 2.6×（1.8–3.4）。之后 27/60 run 又在 prompt 44,143（中位，全部 < hard）处被 ceiling 级 step_compaction（38 次）砍到 29,834。
   - 推断（未测）：(i) 2.6× 超折来自候选可省量的估算 `context_guard.fallback_estimate`（ASCII 字符 // 4 + 非 ASCII 字符数）低估填充观察的真实 token 数——rev 臂日志一例：释放 10,891 字符对应 53,261 → 47,211，约 0.56 token/字符；(ii) 57K 在 < hard 时仍出现 ceiling 级 step_compaction，说明 W2 读到的守卫级别在一轮内停留在本轮最高级（ceiling），不随 fold 后的实测回落。
9. **成本：只有墙钟可用（f）。** full_renders：L0 两臂各 101/140（÷ 0.72，不是每次生成都触发整段渲染）；填充档（49K/57K）两臂都 = 生成次数（÷ 1.00），不是缓存信号；远端 reused_prefix 在 fold 后紧接的生成仍报 0.989 × prompt（8 次，最小 0.968）——fold 从最旧观察起改写 prompt，真前缀缓存此处应大面积失配，所以该字段不反映真实命中（推断），全量重预填充计数本批无法从字段给出。minimal 的重折代价没有在墙钟上显形（49K +1.1%）。
10. **其它（A、I、J、U1、W2、W4）。** aborted 两臂各 38/140（回合上限为主）；recall（未 aborted）none 41/41、41/41，rev 42/43、38/39；57K 回读（file_extract `spill:`）rev 8 次 / 3 run。none 拐点按本批 L0 2 rep 为 49152（w12eval 6 rep 定的 32768），两种口径下 49K / 57K 都在拐点以上。none 跨批（w12eval → 本批）两填充档合并成功率 −3.3 [−10.0,+2.5]、Pass^2 −5.3 [−13.0,+1.7]，在噪声内。

## 2. 口径缺陷与异常
- **analyze_p2.py 本轮改动（已过一审，修补见 fixup）**：`--arm-b NAME`（缺省 `orch-turn-w1w2`；不配 `--two-arm` 时报错）进 layout_arms 必需臂、E 节 `none:<arm-b>` 对、Q 节说明行、sec_v 的 V4 第二臂与标题、复跑行；另两处为跑通本批的最小修补：sec_u 占位在缺 w1w2 时也补空（U3 首表照旧被 strip_u3_catalog 换掉），V4 m=3 格的 m=2 对照在零分布为空时写「—」（原来 `None:.1%` 抛 TypeError）。`backend/tests/test_context_knee_analyze_p2.py` 加 1 条（缺 `--arm-b` 仍报缺 w1w2、`--arm-b` 不配 `--two-arm` 报错、V4 / E 按新臂名、L0 2 rep 不崩、O.TURN 复原）。回归：w12eval 用改后脚本重跑，MD 对改前脚本（HEAD）除复跑行（输出路径）外 0 差；对档案 ANALYSIS.md 另差 J 节 4 行「（fold full）」——那是 99076c5a 的 analyze_orch 归一显示，与本次改动无关；CSV 与档案逐字节相同。一审修补（见 `d48_evidence.py` 同批 fixup）：V5「拷贝续跑批」警告改按 `--p2` 批同 (level,item,rep) 且 stamp 相同的行数判定（`copied_resume_rows`，不再只看 `--two-arm`）；U3 占位改成 CAT / W12 两个键各自独立补空（不依赖 has_cat，否则有 catalog、`--arm-b` 缺字面 w1w2 时对 `aggs["orch-turn-w1w2"]` KeyError）；新增 `--arm-b orch-turn-none` 自配报错。`backend/tests/test_context_knee_analyze_p2.py` 再加 4 条（`copied_resume_rows` 单测、V5 告警按拷贝行门禁、U3 占位独立于 has_cat 且变异体复现 KeyError、`--arm-b` 自配报错）。回归：p2（五臂缺省）/ p2b（四臂缺省）/ w12eval（`--two-arm`）三批缺省路径用 HEAD 副本与工作树脚本重跑，MD（除复跑行）与 CSV 均 cmp 相同。
- **自动部分有写死的标签 / 不适用的段落**（数字正确或不可用，读法见此）：V5 首行「⚠️ 拷贝续跑批……」不适用（本批无拷贝行，Σwall 全部是本 VM 新跑）；I 节「§2 目标比值」表为空（O.TURN 收窄到在场的 none，rev 作额外臂只进首表与事件表）——比值以证据 c1–c3 为准；I 节标题「三臂」、K 节标题「orch-turn-w1 对 orch-turn-w1w2」本批只有 none + rev；V4 / W2 的「三填充档合并」= 本批两填充档合并；E 节「每题满分 = 共同档数 × 2」在 6 rep 档不对（按题计数本身正确）；V2c / V3b / V4 / W2 的尾概率与判定全部「超出支撑」（L0 2 rep 无零分布），噪声判读以证据 d 为准；J 节 none 的「24576/39322/53248（fold full）」是 guard off 臂记下的配置值，不生效。
- **零分布与委托口径不同**：委托写「m=3 门槛 23/27」，那是 p2b none+full L0 的值（本次复现）；本批同法只能用填充档臂内 6 rep（49K 23/23、57K 27/20、合并 23/23）。填充档臂内零分布含该档自身的题级通过率结构，与 L0 零分布不是同一总体（推断：偏向本档实际噪声，更贴切）。填充档臂内零分布未利用同 VM 共同随机数（49K 27/60 全程逐字相同），门槛偏保守（V1d）。
- **§2 第一条判据数**：§2 原文两项都是 90%；委托写的「≥ 0.75」在 §2 里没有，本稿不用。
- **57K 的 rev 测的仍是 fold 后的 prompt**（首生成中位 41,104，不是 57K）：57K 填充本身超过 ceiling，任何防溢出方案在首生成前都必须折；本批变化只是折得浅了（27,158 → 41,104）。
- **minimal 没做到「只折够用」**：设计意图是量到 < hard 即停，实测停在 hard 下 10,096 tok（57K 中位）/ 3,642 tok（49K 中位），超折 2.6×（e2）；原因推断见 §1.8。
- **reused_prefix 不可信（f、e3 末条）**：方案 §9 起机单预估的「minimal 重折使前缀缓存从最旧处失效」的 prefill 代价，本批字段测不到；只能读墙钟。
- **起机记账**：首次部署未 READY（allocating → stopped，rc=4），stop 回执 `tailnet_cleanup: logout-unconfirmed`——第一台 VM 的 Tailscale 临时节点注销未确认（第二台为 ephemeral-logout-confirmed）；本目录无法核实是否残留节点。主会话本机 `tailscale status` 核实无 agent-compute 残留节点（方案 §9 11-09 晚条）。费用 46.15 单位不在本目录产物里。
- **rev 臂 .log 74 行 ❌**（两臂 .log 各 74 行）：harness 逐 run 判败行（如 L0 lc-hard-003 / 004「步数超限」），与 transcripts 的 score.pass 同源，不是异常。

## §3 建议（主会话）

1. **判定。** §2 第二条（防溢出新口径）✅、第三条 ✅；第一条合并点估 ✅（成功率比 1.00、Pass^2 比 0.99），57K Pass^2 单档比 0.79 点估 ✗，四个单档差（+3.3 / +4.0 / −3.3 / −4.7）全部在本批 m=3 零分布门槛内（§1.4–1.5）。10 题 × 6 rep 的区间 ±15–20 点，判不出 10% 非劣效——这是 w12eval §3 已写明的样本量限制，不是本批特有；要判需约 40 题或约 25 rep，成本 4 倍以上，不建议为此加 rep。按用户 11-08 以点估过门的先例，P2 出口读作「不劣于无管理 + 0 溢出 + 成本不升」达到。
2. **对生产缺省（w12eval w1w2：hard 39,322 + fold full）的方向。** w12eval 合并 −4.4 / −6.4，57K Pass^2 −12.7；本批 +3.3 / +4.0（49K）、−3.3 / −4.7（57K）。方向一致地更好，但跨批跨 VM、none 自身也漂了 −3.3（§1.10），不能当配对证据；要配对须同 VM 三臂（none / w1w2 / rev）再跑一批，约 10 h。本会话不建议：rev 对 none 已回到噪声内，再证「rev 优于 full」只是排序，不改决策。
3. **建议 A：翻生产缺省** `config.yaml` `agent.context_guard.hard_tokens` 39,322 → 51,200、新增 `fold_mode: minimal`（soft 24,576 与 reserve 不动）。护栏角色由「中段收敛提醒 + 整体 fold」改为「纯防溢出」：L0–49K 首生成前零干预（§1.8：49K hard 首生成前 0/60），57K 浅折（27,158 → 41,104），0 溢出、Σwall −5.9%。另一提交过审（config.yaml 是红线路径，主会话亲改；`derive_limits` 要求 hard < ceiling，启动期校验）。
4. **建议 B：D48b（小改，可不挡翻缺省）。** minimal 超折 2.6× 的原因（推断，代码核对一致）：`_minimal_fold_candidates` 用 `fallback_estimate`（ASCII // 4）估可省量，填充观察实测 ≈ 0.56 token/字符，低估约 2.2×，首批就凑过头。修法：按本轮实测密度校准估值（measured ÷ fallback_estimate(全 history)），或首批只凑缺口一半、余下靠 remeasure 迭代。目标 57K 首生成 41K → ≈ 50K。建议翻缺省不等它：现状已优于 full；D48b 随下一批测。
5. **测量侧。** 全量重预填充计数本批给不出（reused_prefix 在 fold 后紧接生成仍报 0.989×，full_renders = 生成次数，§1.9）——方案 §9 起机单预估的 prefill 代价只在墙钟上可见（不升反降）。W1/W2 归因（w1 单独臂）仍未做，留给下一批与 D48b 同跑。
6. **记账。** 6.95 h / 46.15 unit（余额 555.93）；首台 VM 注销未确认一事，本机 `tailscale status` 无 agent-compute 残留节点（主会话核）。

# 自动部分

<!-- BEGIN AUTO -->
_复跑：`PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python benchmarks/context_knee/analyze_p2.py --data benchmarks/data/context-knee-20261109-d48 --ref benchmarks/data/context-knee-20261031/qwen-current --prev benchmarks/data/context-knee-20261105-d44 --b2 benchmarks/data/context-knee-20261102-b2 --p2 benchmarks/data/context-knee-20261108-w12eval --two-arm --arm-b orch-turn-w1w2-rev --out-md benchmarks/data/context-knee-20261109-d48/ANALYSIS.md --out-csv benchmarks/data/context-knee-20261109-d48/summary_by_level.csv`_

_自动生成：`benchmarks/context_knee/analyze_p2.py`（P2 布局：A–F 节 = analyze.render 原样，自动对照关闭、E 节只列指定对；I/J/K/M/N/O/P/Q 节 = analyze_orch 原样，R/T 节本批无数据不渲染；U 节 = analyze_d44 原样（只列本批）；V 节新增；W 节 = 对 --p2 批；F 节生产前缀取 orch-turn-none 行内 prefix_base_tokens 6051；`@d44` = --prev 批同名臂）；规则 metric=pass_hat_2 drop=0.2 recover=0.1 基线候选=[0, 4096]；重复行保留 latest（非 error 优先）；bootstrap 10000 次 seed 20261031：Pass^2 只按题重抽（题内 rep 对保持完整），成功率同样只按题重抽（2026-11-07 起；此前两段）；相对基线一律限定共同题集。采样 card(sent …) = 已发送、未回执核验。_

### A. 输入与去重

| 组 | 模型 | 采样 | MTP(按目录名) | 原始行 | 去重后有效 | 缺测(error) | 丢弃重复行 | 重复行 pass 分歧 | wall 不可用档 | 全组复读命中(run) | 全组 aborted |
|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen-current | Qwen3.8-27B-INT4 | current | 关 | 80 | 80 | 0 | 0 | 0 | 0 | 0(0) | 0 |
| orch-turn-none | Qwen3.8-27B-INT4 | current | 关 | 140 | 140 | 0 | 0 | 0 | — | 5(4) | 38 |
| orch-turn-w1w2-rev | Qwen3.8-27B-INT4 | current | 关 | 140 | 140 | 0 | 0 | 0 | — | 4(3) | 38 |
| orch-turn-none@d44 | Qwen3.8-27B-INT4 | current | 关 | 80 | 80 | 0 | 0 | 0 | — | 1(1) | 30 |


### B. 每档汇总（成功率单位 run，Pass^2 单位 题；区间 Wilson 95%）

| 组 | level | n | 缺测 | 成功率 [CI] | Pass^2 [CI] (题数) | Pass@2 | recall (分母) | recall_src 桩/回读/原文 | 回读调用(run) | 前/中/后 | recall漏：部分/零命中且无收尾 | 冗余均 | env错均 | 复读命中(run) | aborted | 连贯 (分母) | 首prompt中位 | 峰值prompt中位 | prefill秒中位 | wall中位 | W2压缩中位 | 全量渲染中位 | ceiling终止 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen-current | 0 | 20 | 0 | 90% [70,97] | 90% [60,98] (10) | 90% | — (0) | 0/0/0 | 0(0) | —/—/— | 0/0 | 0.30 | 0.20 | 0(0) | 0 | 90% (20) | 526 | — | 0.2 | 61.2* | — | — | — |
| qwen-current | 16384 | 20 | 0 | 80% [58,92] | 70% [40,89] (10) | 90% | 100% (20) | 0/0/60 | 0(0) | 100%/100%/100% | 0/0 | 0.10 | 0.20 | 0(0) | 0 | 95% (20) | 16475 | — | 5.8 | 25.1 | — | — | — |
| qwen-current | 32768 | 20 | 0 | 90% [70,97] | 90% [60,98] (10) | 90% | 100% (20) | 0/0/60 | 0(0) | 100%/100%/100% | 0/0 | 0.15 | 0.20 | 0(0) | 0 | 95% (20) | 32821 | — | 13.2 | 33.9 | — | — | — |
| qwen-current | 49152 | 20 | 0 | 90% [70,97] | 90% [60,98] (10) | 90% | 100% (20) | 0/0/60 | 0(0) | 100%/100%/100% | 0/0 | 0.20 | 0.25 | 0(0) | 0 | 85% (20) | 49254 | — | 22.4 | 44.2 | — | — | — |
| orch-turn-none | 0 | 20 | 0 | 65% [43,82] | 50% [24,76] (10) | 80% | — (0) | 0/0/0 | 0(0) | —/—/— | 0/0 | 0.55 | 0.60 | 0(0) | 0 | 100% (20) | 6194 | 7471 | 0.5 | 25.3 | 0.0 | 4.0 | 0 |
| orch-turn-none | 49152 | 60 | 0 | 45% [33,58] | 23% [7,54] (10) | 67% | 100% (41) | 0/0/126 | 0(0) | 100%/100%/100% | 0/0 | 1.42 | 0.92 | 2(1) | 19 | 100% (43) | 49382 | 52359 | 20.8 | 100.1 | 0.0 | 10.5 | 0 |
| orch-turn-none | 57344 | 60 | 0 | 43% [32,56] | 23% [7,54] (10) | 64% | 100% (41) | 0/0/123 | 0(0) | 100%/100%/100% | 0/0 | 1.30 | 1.23 | 3(3) | 19 | 100% (41) | 57554 | 59345 | 26.9 | 86.3 | 0.0 | 10.5 | 0 |
| orch-turn-w1w2-rev | 0 | 20 | 0 | 65% [43,82] | 50% [24,76] (10) | 80% | — (0) | 0/0/0 | 0(0) | —/—/— | 0/0 | 0.55 | 0.60 | 0(0) | 0 | 100% (20) | 6194 | 7471 | 0.5 | 25.7 | 0.0 | 4.0 | 0 |
| orch-turn-w1w2-rev | 49152 | 60 | 0 | 48% [36,61] | 27% [9,57] (10) | 70% | 98% (43) | 48/0/77 ?1 | 0(0) | 98%/98%/98% | 0/0 | 0.95 | 0.80 | 2(1) | 17 | 100% (44) | 49382 | 51222 | 21.1 | 99.7 | 0.0 | 10.0 | 0 |
| orch-turn-w1w2-rev | 57344 | 60 | 0 | 40% [29,53] | 18% [5,49] (10) | 62% | 97% (39) | 82/1/30 ?1 | 8(3) | 97%/97%/97% | 0/0 | 1.17 | 0.93 | 2(2) | 21 | 100% (39) | 41104 | 43396 | 16.0 | 87.1 | 0.0 | 11.0 | 0 |
| orch-turn-none@d44 | 0 | 20 | 0 | 35% [18,57] | 20% [6,51] (10) | 50% | — (0) | 0/0/0 | 0(0) | —/—/— | 0/0 | 1.10 | 0.80 | 0(0) | 3 | 100% (18) | 13900 | 15386 | 0.5 | 31.8 | 0.0 | 8.0 | 0 |
| orch-turn-none@d44 | 32768 | 20 | 0 | 30% [15,52] | 10% [2,40] (10) | 50% | 83% (12) | 0/0/30 | 0(0) | 83%/83%/83% | 0/0 | 3.25 | 0.55 | 0(0) | 8 | 100% (12) | 32866 | 34912 | 8.9 | 60.9 | 0.0 | 11.0 | 0 |
| orch-turn-none@d44 | 49152 | 20 | 0 | 30% [15,52] | 10% [2,40] (10) | 50% | 100% (11) | 0/0/39 | 0(0) | 100%/100%/100% | 0/0 | 1.05 | 1.30 | 1(1) | 9 | 100% (13) | 49314 | 51292 | 18.0 | 78.9 | 0.0 | 11.0 | 0 |
| orch-turn-none@d44 | 57344 | 20 | 0 | 35% [18,57] | 20% [6,51] (10) | 50% | 100% (10) | 0/0/33 | 0(0) | 100%/100%/100% | 0/0 | 0.85 | 1.40 | 0(0) | 10 | 100% (11) | 57530 | 59550 | 23.3 | 82.8 | 0.0 | 11.0 | 0 |

`*` = wall 因并发不可用（已知清单或同档多 stamp 重复行自动检测）。

recall_src = 末轮正文里出现的卡值按来源计数（单位 = 卡值，不是 run；只命中一两张卡的 run 也计；全部 run 合计、含 aborted；`?n` = 来源不明），回读调用 = `file_extract(path="spill:…")` 次数（有回读的 run 数）；口径见 analyze.recall_source。D43 (a) 之后桩上的线索可以直接带出卡值，recall 升高要连同这两列读。

不连贯 run 的归因（判据触发项:题 ×次；不判语义）：

- qwen-current：0: 假名/谚文:lc-hard-001×2；16384: en题汉字作答:lc-hard-004×1；32768: en题汉字作答:lc-hard-004×1；49152: en题汉字作答:lc-hard-004×2、en题汉字作答:lc-hard-009×1
- orch-turn-none：无
- orch-turn-w1w2-rev：无
- orch-turn-none@d44：无

串台与异常收尾（每档：末轮正文提及填充路径 `legacy-2025q3` 的 run / 零步即终的 run / 回忆零命中但有收尾答复的 run）：

- qwen-current：0: 0/0/0；16384: 0/0/0；32768: 0/0/0；49152: 1/0/0
- orch-turn-none：0: 0/0/0；49152: 0/1/0；57344: 1/1/0
- orch-turn-w1w2-rev：0: 0/0/0；49152: 1/1/1；57344: 1/0/1
- orch-turn-none@d44：0: 0/0/0；32768: 0/0/2；49152: 0/0/0；57344: 0/0/0

### C. 拐点判定与相对基线的差

**qwen-current**：到 49152 未见拐点（基线 0 档 90%）

基线档题数：Pass^2 10 题 / 成功率 10 题；差一律在共同题集上算。

| level | Pass^2 (本档题数) | 共同题数 | 配对差 [题级 bootstrap 95%] | 题级 升/降 | 符号检验 p | Wilson 区间与基线重叠 | 成功率 (本档题数) | 成功率共同题数 | 成功率配对差 [题级 bootstrap 95%] |
|---|---|---|---|---|---|---|---|---|---|
| 16384 | 70% (10) | 10 | -20 [-50,+0] | 0/2 | 0.50 | 重叠 | 80% (10) | 10 | -10 [-25,+0] |
| 32768 | 90% (10) | 10 | +0 [+0,+0] | 0/0 | — | 重叠 | 90% (10) | 10 | +0 [+0,+0] |
| 49152 | 90% (10) | 10 | +0 [+0,+0] | 0/0 | — | 重叠 | 90% (10) | 10 | +0 [+0,+0] |

分段合并（按题聚类，低段 [0] → 高段 ≥16384）：Pass^2 -7 [-17,+0]，题级升/降 0/2 p=0.50；成功率 -3 [-8,+0]，升/降 0/2 p=0.50（10 题）

**orch-turn-none**：拐点 49152（基线 0 档 50%）

基线档题数：Pass^2 10 题 / 成功率 10 题；差一律在共同题集上算。

| level | Pass^2 (本档题数) | 共同题数 | 配对差 [题级 bootstrap 95%] | 题级 升/降 | 符号检验 p | Wilson 区间与基线重叠 | 成功率 (本档题数) | 成功率共同题数 | 成功率配对差 [题级 bootstrap 95%] |
|---|---|---|---|---|---|---|---|---|---|
| 49152 | 23% (10) | 10 | -27 [-63,+11] | 3/5 | 0.73 | 重叠 | 45% (10) | 10 | -20 [-45,+5] |
| 57344 | 23% (10) | 10 | -27 [-65,+9] | 4/4 | 1.00 | 重叠 | 43% (10) | 10 | -22 [-50,+5] |

分段合并（按题聚类，低段 [0] → 高段 ≥16384）：Pass^2 -27 [-64,+9]，题级升/降 4/5 p=1.00；成功率 -21 [-47,+4]，升/降 4/6 p=0.75（10 题）

**orch-turn-w1w2-rev**：拐点 49152（基线 0 档 50%）

基线档题数：Pass^2 10 题 / 成功率 10 题；差一律在共同题集上算。

| level | Pass^2 (本档题数) | 共同题数 | 配对差 [题级 bootstrap 95%] | 题级 升/降 | 符号检验 p | Wilson 区间与基线重叠 | 成功率 (本档题数) | 成功率共同题数 | 成功率配对差 [题级 bootstrap 95%] |
|---|---|---|---|---|---|---|---|---|---|
| 49152 | 27% (10) | 10 | -23 [-63,+19] | 4/5 | 1.00 | 重叠 | 48% (10) | 10 | -17 [-43,+10] |
| 57344 | 18% (10) | 10 | -32 [-58,-9] | 3/5 | 0.73 | 重叠 | 40% (10) | 10 | -25 [-42,-7] |

分段合并（按题聚类，低段 [0] → 高段 ≥16384）：Pass^2 -28 [-59,+4]，题级升/降 5/5 p=1.00；成功率 -21 [-42,-0]，升/降 4/6 p=0.75（10 题）

**orch-turn-none@d44**：基线 20% ≤ drop 20%：基线在地板，规则空判（到 57344 未判）（基线 0 档 20%）

基线档题数：Pass^2 10 题 / 成功率 10 题；差一律在共同题集上算。

| level | Pass^2 (本档题数) | 共同题数 | 配对差 [题级 bootstrap 95%] | 题级 升/降 | 符号检验 p | Wilson 区间与基线重叠 | 成功率 (本档题数) | 成功率共同题数 | 成功率配对差 [题级 bootstrap 95%] |
|---|---|---|---|---|---|---|---|---|---|
| 32768 | 10% (10) | 10 | -10 [-40,+20] | 1/2 | 1.00 | 重叠 | 30% (10) | 10 | -5 [-25,+15] |
| 49152 | 10% (10) | 10 | -10 [-30,+0] | 0/1 | 1.00 | 重叠 | 30% (10) | 10 | -5 [-25,+15] |
| 57344 | 20% (10) | 10 | +0 [-30,+30] | 1/1 | 1.00 | 重叠 | 35% (10) | 10 | +0 [-30,+30] |

分段合并（按题聚类，低段 [0] → 高段 ≥16384）：Pass^2 -7 [-33,+13]，题级升/降 2/2 p=1.00；成功率 -3 [-25,+18]，升/降 3/4 p=1.00（10 题）

敏感性（拐点档；`—` = 未见拐点；基线列 max = 0/4096 取高者）：

| 组 | P^2/max/15 | P^2/max/20 | P^2/max/25 | P^2/0/15 | P^2/0/20 | P^2/0/25 | P^2/4096/15 | P^2/4096/20 | P^2/4096/25 | 成功/max/15 | 成功/max/20 | 成功/max/25 | 成功/0/15 | 成功/0/20 | 成功/0/25 | 成功/4096/15 | 成功/4096/20 | 成功/4096/25 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| qwen-current | — | — | — | — | — | — | n/a | n/a | n/a | — | — | — | — | — | — | n/a | n/a | n/a |
| orch-turn-none | 49152 | 49152 | 49152 | 49152 | 49152 | 49152 | n/a | n/a | n/a | 49152 | 49152 | — | 49152 | 49152 | — | n/a | n/a | n/a |
| orch-turn-w1w2-rev | 49152 | 49152 | 57344 | 49152 | 49152 | 57344 | n/a | n/a | n/a | 49152 | 57344 | 57344 | 49152 | 57344 | 57344 | n/a | n/a | n/a |
| orch-turn-none@d44 | — | — | — | — | — | — | n/a | n/a | n/a | — | — | — | — | — | — | n/a | n/a | n/a |

### D. 按题分解（每格 = 通过 rep / rep 数；低段 = [0, 4096]，高段 = ≥16384）

**qwen-current**

| 题 | 0 | 16384 | 32768 | 49152 | 低段 | 高段 | 全部 | 归类 |
|---|---|---|---|---|---|---|---|---|
| lc-hard-001 | 2/2 | 2/2 | 2/2 | 2/2 | 100% | 100% | 100% | 稳定通过 |
| lc-hard-002 | 2/2 | 2/2 | 2/2 | 2/2 | 100% | 100% | 100% | 稳定通过 |
| lc-hard-003 | 2/2 | 1/2 | 2/2 | 2/2 | 100% | 83% | 88% | 本来就不稳 |
| lc-hard-004 | 2/2 | 2/2 | 2/2 | 2/2 | 100% | 100% | 100% | 稳定通过 |
| lc-hard-005 | 2/2 | 2/2 | 2/2 | 2/2 | 100% | 100% | 100% | 稳定通过 |
| lc-hard-006 | 2/2 | 2/2 | 2/2 | 2/2 | 100% | 100% | 100% | 稳定通过 |
| lc-hard-007 | 2/2 | 2/2 | 2/2 | 2/2 | 100% | 100% | 100% | 稳定通过 |
| lc-hard-008 | 2/2 | 2/2 | 2/2 | 2/2 | 100% | 100% | 100% | 稳定通过 |
| lc-hard-009 | 0/2 | 0/2 | 0/2 | 0/2 | 0% | 0% | 0% | 稳定失败 |
| lc-hard-010 | 2/2 | 1/2 | 2/2 | 2/2 | 100% | 83% | 88% | 本来就不稳 |

**orch-turn-none**

| 题 | 0 | 49152 | 57344 | 低段 | 高段 | 全部 | 归类 |
|---|---|---|---|---|---|---|---|
| lc-hard-001 | 2/2 | 1/6 | 1/6 | 100% | 17% | 29% | 随长度失败 |
| lc-hard-002 | 2/2 | 3/6 | 2/6 | 100% | 42% | 50% | 本来就不稳 |
| lc-hard-003 | 0/2 | 1/6 | 2/6 | 0% | 25% | 21% | 本来就不稳 |
| lc-hard-004 | 1/2 | 2/6 | 3/6 | 50% | 42% | 43% | 本来就不稳 |
| lc-hard-005 | 2/2 | 4/6 | 6/6 | 100% | 83% | 86% | 本来就不稳 |
| lc-hard-006 | 0/2 | 1/6 | 1/6 | 0% | 17% | 14% | 本来就不稳 |
| lc-hard-007 | 2/2 | 3/6 | 1/6 | 100% | 33% | 43% | 本来就不稳 |
| lc-hard-008 | 2/2 | 2/6 | 2/6 | 100% | 33% | 43% | 本来就不稳 |
| lc-hard-009 | 1/2 | 5/6 | 5/6 | 50% | 83% | 79% | 本来就不稳 |
| lc-hard-010 | 1/2 | 5/6 | 3/6 | 50% | 67% | 64% | 本来就不稳 |

**orch-turn-w1w2-rev**

| 题 | 0 | 49152 | 57344 | 低段 | 高段 | 全部 | 归类 |
|---|---|---|---|---|---|---|---|
| lc-hard-001 | 2/2 | 1/6 | 2/6 | 100% | 25% | 36% | 随长度失败 |
| lc-hard-002 | 2/2 | 3/6 | 4/6 | 100% | 58% | 64% | 本来就不稳 |
| lc-hard-003 | 0/2 | 1/6 | 2/6 | 0% | 25% | 21% | 本来就不稳 |
| lc-hard-004 | 1/2 | 2/6 | 1/6 | 50% | 25% | 29% | 本来就不稳 |
| lc-hard-005 | 2/2 | 4/6 | 5/6 | 100% | 75% | 79% | 本来就不稳 |
| lc-hard-006 | 0/2 | 2/6 | 0/6 | 0% | 17% | 14% | 本来就不稳 |
| lc-hard-007 | 2/2 | 3/6 | 2/6 | 100% | 42% | 50% | 本来就不稳 |
| lc-hard-008 | 2/2 | 2/6 | 4/6 | 100% | 50% | 57% | 本来就不稳 |
| lc-hard-009 | 1/2 | 6/6 | 2/6 | 50% | 67% | 64% | 本来就不稳 |
| lc-hard-010 | 1/2 | 5/6 | 2/6 | 50% | 58% | 57% | 本来就不稳 |

**orch-turn-none@d44**

| 题 | 0 | 32768 | 49152 | 57344 | 低段 | 高段 | 全部 | 归类 |
|---|---|---|---|---|---|---|---|---|
| lc-hard-001 | 0/2 | 0/2 | 0/2 | 0/2 | 0% | 0% | 0% | 稳定失败 |
| lc-hard-002 | 2/2 | 1/2 | 1/2 | 0/2 | 100% | 33% | 50% | 本来就不稳 |
| lc-hard-003 | 0/2 | 1/2 | 1/2 | 2/2 | 0% | 67% | 50% | 本来就不稳 |
| lc-hard-004 | 0/2 | 0/2 | 0/2 | 0/2 | 0% | 0% | 0% | 稳定失败 |
| lc-hard-005 | 2/2 | 1/2 | 2/2 | 2/2 | 100% | 83% | 88% | 本来就不稳 |
| lc-hard-006 | 0/2 | 0/2 | 1/2 | 0/2 | 0% | 17% | 12% | 本来就不稳 |
| lc-hard-007 | 1/2 | 1/2 | 0/2 | 1/2 | 50% | 33% | 38% | 本来就不稳 |
| lc-hard-008 | 0/2 | 0/2 | 0/2 | 0/2 | 0% | 0% | 0% | 稳定失败 |
| lc-hard-009 | 1/2 | 2/2 | 1/2 | 1/2 | 50% | 67% | 62% | 本来就不稳 |
| lc-hard-010 | 1/2 | 0/2 | 0/2 | 1/2 | 50% | 17% | 25% | 本来就不稳 |

失败原因（主因按 复读命中 > aborted > 超步数 > 无收尾答复 > 终态断言 > 其他 取首个；括号内为多标签计数）：

| 组 | level | 失败 run | 复读 | aborted | 超步数 | 无收尾答复 | 终态断言 | 其他 |
|---|---|---|---|---|---|---|---|---|
| qwen-current | 0 | 2 | 0(0) | 0(0) | 0(0) | 0(0) | 2(2) | 0(0) |
| qwen-current | 16384 | 4 | 0(0) | 0(0) | 0(0) | 0(0) | 4(4) | 0(0) |
| qwen-current | 32768 | 2 | 0(0) | 0(0) | 0(0) | 0(0) | 2(2) | 0(0) |
| qwen-current | 49152 | 2 | 0(0) | 0(0) | 0(0) | 0(0) | 2(2) | 0(0) |
| qwen-current | 合计 | 10 | 0(0) | 0(0) | 0(0) | 0(0) | 10(10) | 0(0) |
| orch-turn-none | 0 | 7 | 0(0) | 0(0) | 5(5) | 0(0) | 2(2) | 0(0) |
| orch-turn-none | 49152 | 33 | 1(1) | 18(19) | 10(28) | 0(17) | 4(18) | 0(0) |
| orch-turn-none | 57344 | 34 | 3(3) | 17(19) | 10(26) | 0(19) | 4(19) | 0(0) |
| orch-turn-none | 合计 | 74 | 4(4) | 35(38) | 25(59) | 0(36) | 10(39) | 0(0) |
| orch-turn-w1w2-rev | 0 | 7 | 0(0) | 0(0) | 5(5) | 0(0) | 2(2) | 0(0) |
| orch-turn-w1w2-rev | 49152 | 31 | 1(1) | 16(17) | 10(24) | 0(16) | 4(16) | 0(0) |
| orch-turn-w1w2-rev | 57344 | 36 | 2(2) | 19(21) | 13(33) | 0(21) | 2(15) | 0(0) |
| orch-turn-w1w2-rev | 合计 | 74 | 3(3) | 35(38) | 28(62) | 0(37) | 8(33) | 0(0) |
| orch-turn-none@d44 | 0 | 13 | 0(0) | 3(3) | 10(12) | 0(2) | 0(3) | 0(0) |
| orch-turn-none@d44 | 32768 | 14 | 0(0) | 8(8) | 5(11) | 0(8) | 1(6) | 0(0) |
| orch-turn-none@d44 | 49152 | 14 | 1(1) | 8(9) | 4(11) | 0(7) | 1(6) | 0(0) |
| orch-turn-none@d44 | 57344 | 13 | 0(0) | 10(10) | 3(10) | 0(9) | 0(6) | 0(0) |
| orch-turn-none@d44 | 合计 | 54 | 1(1) | 29(30) | 22(44) | 0(26) | 2(21) | 0(0) |

### E. 对照（b − a，共同档位、共同题集；Pass^2 与成功率都题级 bootstrap 95%；正 = b 更好）

> 采样对照：card(sent …) 只说明 harness 发送了这些键；**采样键是否在远端生效未经回执核验**。

| 对照 | level | 成功率差 [配对95%] | Pass^2 差 [配对95%] | 题级 升/降 p | recall a→b | 复读命中 a→b | 连贯 a→b | aborted a→b | env错均 a→b |
|---|---|---|---|---|---|---|---|---|---|
| orch-turn-none@d44→orch-turn-none（指定） | 0 | +30 [+5,+55] | +30 [+0,+60] | 4/0 p=0.12 | —→— | 0→0 | 100%→100% | 3→0 | 0.80→0.60 |
| orch-turn-none@d44→orch-turn-none（指定） | 49152 | +15 [-8,+38] | +13 [-9,+33] | 6/3 p=0.51 | 100%→100% | 1→2 | 100%→100% | 9→19 | 1.30→0.92 |
| orch-turn-none@d44→orch-turn-none（指定） | 57344 | +8 [-13,+27] | +3 [-23,+23] | 6/2 p=0.29 | 100%→100% | 0→3 | 100%→100% | 10→19 | 1.40→1.23 |
| orch-turn-none@d44→orch-turn-none（指定） | 合并(按题聚类) | +18 [+1,+33] | +15 [-1,+30] | 7/3 p=0.344 | 100%→100% | 1→5 | | 22→38 | |
| orch-turn-none→orch-turn-w1w2-rev（指定） | 0 | +0 [+0,+0] | +0 [+0,+0] | 0/0 p=— | —→— | 0→0 | 100%→100% | 0→0 | 0.60→0.60 |
| orch-turn-none→orch-turn-w1w2-rev（指定） | 49152 | +3 [+0,+8] | +4 [+0,+11] | 2/0 p=0.50 | 100%→98% | 2→2 | 100%→100% | 19→17 | 0.92→0.80 |
| orch-turn-none→orch-turn-w1w2-rev（指定） | 57344 | -3 [-20,+13] | -5 [-23,+12] | 4/5 p=1.00 | 100%→97% | 3→2 | 100%→100% | 19→21 | 1.23→0.93 |
| orch-turn-none→orch-turn-w1w2-rev（指定） | 合并(按题聚类) | -0 [-5,+5] | -0 [-5,+4] | 4/4 p=1.000 | 100%→98% | 5→4 | | 38→38 | |

按题通过 run 数（共同档合并，a→b；每题满分 = 共同档数 × 2）：

- orch-turn-none@d44→orch-turn-none（指定）：hard-001 0→4，hard-002 3→7，hard-003 3→3，hard-004 0→6，hard-005 6→12，hard-006 1→2，hard-007 2→6，hard-008 0→6，hard-009 3→11，hard-010 2→9
- orch-turn-none→orch-turn-w1w2-rev（指定）：hard-001 4→5，hard-002 7→9，hard-003 3→3，hard-004 6→4，hard-005 12→11，hard-006 2→2，hard-007 6→7，hard-008 6→8，hard-009 11→9，hard-010 9→8

### F. D1/D2 派生（soft = 拐点 × 0.5，hard = 拐点 × 0.8；单位 = 首次生成 prompt 总 token）

ceiling = max_model_len 65536 − 生成预留 12288 = 53248（远端）。生产 system+tools 前缀 6051 tok（取 orch-turn-none 行内字段 prefix_base_tokens，见上）；harness 基础 prompt 见 B 表 level 0 的首 prompt 中位。

| 组 | 拐点（默认规则） | 敏感性范围（P^2 与成功率、各基线、drop 15–25） | soft | hard | hard 与生产前缀之差 | 敏感性上沿 soft/hard | 备注 |
|---|---|---|---|---|---|---|---|
| qwen-current | 未见（≤49152） | 全部变体未见拐点 | ≥24576 | ≥39322 | +33271 | — | 下限：来自测量顶档，不是拐点；hard 对 ceiling -13926 |
| orch-turn-none | 49152 | 49152–49152 | 24576 | 39322 | +33271 | 24576/39322 | |
| orch-turn-w1w2-rev | 49152 | 49152–57344 | 24576 | 39322 | +33271 | 28672/45875 | |
| orch-turn-none@d44 | 未见（≤57344） | 全部变体未见拐点 | ≥28672 | ≥45875 | +39824 | — | 下限：来自测量顶档，不是拐点；hard 对 ceiling -7373 |


### I. P2 出口：turn 放置三臂（无管理 → +W1 → +W1+W2；prefix prod）；首表与事件表并入 orch-turn-w1w2-rev（比值表只比 prod 三臂）

orch-turn-none 拐点（analyze.py 默认规则 metric=pass_hat_2 drop=0.2）：**拐点 49152**，基线 0 档 50%；成功率口径：拐点 49152，基线 65%。基线 Pass^2 50% 时「降 20 点」意味着降到 30%——规则在地板附近几乎不可触发。

| 组 | level | n(缺测) | 成功率 | Pass^2 | 峰值字段中位 | 实发峰值中位 / 最大 | 首prompt中位 | 生成次数中位 | full_renders 总/中位/÷生成 | update_plan/complete_step/remember | aborted 原因 | warning 状态 | Σwall s | wall中位 | system_errors | dup_blocked |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 0 | 20(0) | 65% | 50% | 7,471 | 7,471 / 11,029 | 6,194 | 6 | 101/4/0.72 | 2/7/2 | 0 | 0 | 488 | 25.3 | 3 | 4 |
| orch-turn-none | 49152 | 60(0) | 45% | 23% | 52,359 | 52,359 / 56,455 | 49,382 | 10 | 576/10/1.00 | 30/87/53 | repetition_guard 1、回合上限 18 | 19 | 5,757 | 100.1 | 2 | 62 |
| orch-turn-none | 57344 | 60(0) | 43% | 23% | 59,345 | 59,345 / 63,360 | 57,554 | 10 | 556/10/1.00 | 22/62/45 | 回合上限 19 | 19 | 5,921 | 86.3 | 2 | 63 |
| orch-turn-w1w2-rev | 0 | 20(0) | 65% | 50% | 7,471 | 7,471 / 11,029 | 6,194 | 6 | 101/4/0.72 | 2/7/2 | 0 | 0 | 489 | 25.7 | 3 | 4 |
| orch-turn-w1w2-rev | 49152 | 60(0) | 48% | 27% | 51,222 | 51,222 / 53,216 | 49,382 | 10 | 567/10/1.00 | 30/87/52 | repetition_guard 1、回合上限 16 | 18 | 5,819 | 99.7 | 2 | 38 |
| orch-turn-w1w2-rev | 57344 | 60(0) | 40% | 18% | 43,396 | 43,396 / 48,770 | 41,104 | 11 | 604/11/1.00 | 30/86/54 | 回合上限 21 | 21 | 5,173 | 87.1 | 0 | 46 |

护栏 / 编排层事件（本臂 run 的 guard_events_lines 切片计数）：

| 组 | level | context_guard_fold | context_hard | context_soft | duplicate_call | failure_route | failure_swallow_nudge | no_plan_nudge | parse_error | premature_finish_nudge | repetition | step_compaction | step_readback_exempt | tool_error | turn_abort | upgrade_triggered | verify_nudge |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 0 | 0 | 0 | 0 | 4 | 0 | 0 | 20 | 1 | 0 | 0 | 0 | 0 | 4 | 0 | 0 | 2 |
| orch-turn-none | 49152 | 0 | 0 | 0 | 62 | 9 | 0 | 59 | 1 | 3 | 2 | 0 | 0 | 7 | 19 | 17 | 6 |
| orch-turn-none | 57344 | 0 | 0 | 0 | 63 | 7 | 0 | 59 | 0 | 0 | 3 | 0 | 0 | 8 | 19 | 17 | 5 |
| orch-turn-w1w2-rev | 0 | 0 | 0 | 0 | 4 | 0 | 0 | 20 | 1 | 0 | 0 | 0 | 0 | 4 | 0 | 0 | 2 |
| orch-turn-w1w2-rev | 49152 | 8 | 30 | 60 | 38 | 9 | 1 | 59 | 1 | 1 | 2 | 44 | 0 | 7 | 17 | 17 | 6 |
| orch-turn-w1w2-rev | 57344 | 60 | 0 | 60 | 46 | 6 | 0 | 60 | 0 | 1 | 2 | 38 | 1 | 6 | 21 | 17 | 4 |

§2 目标比值（管理臂 ÷ 无管理臂，同档；峰值用实发峰值中位；Σwall 含 error 行）：

| 组 | level | 成功率比（≥0.9） | Pass^2 比（≥0.9） | Pass^2 差 | 实发峰值中位 ÷ 无管理 | 实发峰值中位 ÷ level | 实发峰值 ≤ 0.7×level 的 run | Σwall 增量（≤25%） |
|---|---|---|---|---|---|---|---|---|


### J. W1 天花板路径（ceiling = context_limit − reserve，行内字段现算）

| 组 | level | runs | soft/hard/ceiling | ceiling_fold（第 1 次生成前 / 之后） | fold before 区间 | fold after 区间 | fold 压缩条数中位 | context_hard（第 1 次 / 之后） | context_guard 终止 | 峰值字段 > 实发峰值 的 run | 实发 ≥ ceiling 的生成 | 实发最大 | recall |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 49152 | 60 | 24576/39322/53248（fold full） | 0 / 0 | —–— | —–— | — | 0 / 0 | 0 | 0 | 101 | 56,455 | 100% |
| orch-turn-none | 57344 | 60 | 24576/39322/53248（fold full） | 0 / 0 | —–— | —–— | — | 0 / 0 | 0 | 0 | 556 | 63,360 | 100% |
| orch-turn-w1w2-rev | 49152 | 60 | 24576/51200/53248（fold minimal） | 0 / 8 | 53,261–53,591 | 45,362–49,485 | 14 | 0 / 30 | 0 | 0 | 0 | 53,216 | 98% |
| orch-turn-w1w2-rev | 57344 | 60 | 24576/51200/53248（fold minimal） | 60 / 0 | 56,693–59,097 | 35,044–46,354 | 35 | 0 / 0 | 0 | 0 | 0 | 48,770 | 97% |

fold 之后模型做了什么（按 run 计；rag_search / file_extract 是生产工具名：rag_search 在本批是 W-K2 可用桩（回生产空结果形态）；file_extract 只回读 `spill:` 句柄；System Error = 编排层拦截回执）：

| 组 | level | runs | 调 rag_search 的 run | 收到 System Error 的 run（其中 aborted） | System Error 回执合计 | 调 file_extract 的 run | spill_readbacks 合计 | aborted 合计 |
|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 49152 | 60 | 3 | 2（0） | 2 | 0 | 0 | 19 |
| orch-turn-none | 57344 | 60 | 0 | 2（1） | 2 | 0 | 0 | 19 |
| orch-turn-w1w2-rev | 49152 | 60 | 3 | 2（0） | 2 | 0 | 0 | 17 |
| orch-turn-w1w2-rev | 57344 | 60 | 0 | 0（0） | 0 | 3 | 8 | 21 |

- orch-turn-none：gens 1272 次，有量值 1272 次，引擎 prompt_tokens − 量值 ≠ 0 的 0 次（最大 |差| 0）；measure_source {'proxy': 1272}
- orch-turn-w1w2-rev：gens 1311 次，有量值 1311 次，引擎 prompt_tokens − 量值 ≠ 0 的 0 次（最大 |差| 0）；measure_source {'guard': 1311}

### K. W2 有效性（orch-turn-w1 对 orch-turn-w1w2 / orch-turn-w1w2-rev）

| 组 | level | complete_step 次数 | 有 ≥1 次 complete_step 的 run | update_plan 次数 | step_compaction 事件（按档位） | freed_chars 合计 | turn_metrics.step_compactions 合计 | W2免压回读 | spill_readbacks 合计 |
|---|---|---|---|---|---|---|---|---|---|
| orch-turn-w1w2-rev | 0 | 7 | 2/20 | 2 | 0（—） | 0 | 0 | 0 | 0 |
| orch-turn-w1w2-rev | 49152 | 87 | 29/60 | 30 | 44（ceiling 10、hard 34） | 1,321,209 | 44 | 0 | 0 |
| orch-turn-w1w2-rev | 57344 | 86 | 29/60 | 30 | 38（ceiling 38） | 778,167 | 38 | 1 | 8 |

### M. 数据质量（去重后；error 行不进率；连贯率按 quality.py 现算）

| 组 | 原始行 | 有效 | error 行（原因） | repetition_events 非空 run | 复读命中合计 | 连贯 run/有正文 run | 非 ja 题含假名/谚文的 run | L0 aborted [Wilson] | 全档 aborted |
|---|---|---|---|---|---|---|---|---|---|
| qwen-current | 80 | 80 | 0（—） | 0 | 0 | 73/80 | 2 | 0/20 [0,16] | 0 |
| orch-turn-none | 140 | 140 | 0（—） | 4 | 5 | 104/104 | 0 | 0/20 [0,16] | 38 |
| orch-turn-w1w2-rev | 140 | 140 | 0（—） | 3 | 4 | 103/103 | 0 | 0/20 [0,16] | 38 |

### N. 口径与记账核对（自动检查；非零 = 值得看）

| 组 | 峰值字段≠实发峰值 | full_renders 非 int | full_renders≠turn_metrics | full_renders>生成次数 | Σfull_renders÷Σ生成 | 事件计数≠切片 | 切片 session 不符 | 事件行未被任何 run 覆盖 | fill.prompt_tokens_actual≠首生成 | prefix_base 取值 |
|---|---|---|---|---|---|---|---|---|---|---|
| qwen-current | 0 | 80 | 0 | 0 | — | — | — | — | 0 | [None] |
| orch-turn-none | 0 | 0 | 0 | 0 | 0.97 | 0 | 0 | 0 | 0 | [6051] |
| orch-turn-w1w2-rev | 0 | 0 | 0 | 0 | 0.97 | 0 | 0 | 0 | 0 | [6051] |

- orch-turn-w1w2-rev：第 1 次生成前 fold 的 60 个 run，fill.prompt_tokens_est 中位 57,554，fill.prompt_tokens_actual 中位 41,104（= 压缩后首生成，不是填充实际量）
- run_colab.log 臂起止：orch-turn-none 08:51→12:15（204 min，harness rc=0）；orch-turn-w1w2-rev 12:15→15:27（192 min，harness rc=0）

### O. W-K2 可用桩与推回链（编排器臂 × level；`@d44` = d44 批同名臂，无桩字段记 —）

口径：桩调用 = 行字段 `stub_served` 各名求和；`stub_unknown` = 行字段（绊线，应恒 0）；feed Unknown = 本脚本按 turns[*].feed 逐位重数回执含 `Unknown tool` 的调用（独立复核绊线）；delivery blocked = feed 回执含该串的次数；检索未完成 = `turn_metrics.retrieval_incomplete` 为真的 run；迭代耗尽 = aborted_reason 含「回合上限」；超限中止 = aborted_reason == context_guard。

| 组 | level | n | 成功率 | 工具调用 | 桩调用 | stub_unknown | feed Unknown | stub_failed | system_errors | delivery blocked | 检索未完成 run | 迭代耗尽 run | 超限中止 run |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 0 | 20 | 65% | 193 | 46 | 0 | 0 | 2 | 3 | 0 | 0 | 0 | 0 |
| orch-turn-none | 49152 | 60 | 45% | 694 | 247 | 0 | 0 | 6 | 2 | 0 | 0 | 18 | 0 |
| orch-turn-none | 57344 | 60 | 43% | 680 | 250 | 0 | 0 | 6 | 2 | 0 | 0 | 19 | 0 |
| orch-turn-w1w2-rev | 0 | 20 | 65% | 193 | 46 | 0 | 0 | 2 | 3 | 0 | 0 | 0 | 0 |
| orch-turn-w1w2-rev | 49152 | 60 | 48% | 651 | 219 | 0 | 0 | 6 | 2 | 0 | 0 | 16 | 0 |
| orch-turn-w1w2-rev | 57344 | 60 | 40% | 707 | 269 | 0 | 0 | 6 | 0 | 0 | 0 | 21 | 0 |
| orch-turn-none@d44 | 0 | 20 | 35% | 232 | 56 | 0 | 0 | 2 | 0 | 0 | 0 | 3 | 0 |
| orch-turn-none@d44 | 32768 | 20 | 30% | 275 | 73 | 0 | 0 | 2 | 0 | 0 | 0 | 8 | 0 |
| orch-turn-none@d44 | 49152 | 20 | 30% | 248 | 77 | 0 | 0 | 1 | 0 | 0 | 0 | 9 | 0 |
| orch-turn-none@d44 | 57344 | 20 | 35% | 247 | 97 | 0 | 0 | 1 | 0 | 0 | 0 | 10 | 0 |

桩按工具名（全档合计，降序；括号 = 占该臂工具调用的比例）：

- orch-turn-none：桩 543/1567 调用（35%）：run_shell_command 319、list_dir 90、edit_file 66、run_math 30、trash_file 12、find_files 11、rag_search 9、move_file 3、copy_file 3；stub_unknown 合计 0（非 0 行 0）；stub_failed 合计 14（非 0 行 14）
- orch-turn-w1w2-rev：桩 534/1551 调用（34%）：run_shell_command 315、list_dir 90、edit_file 70、run_math 28、find_files 10、trash_file 9、rag_search 7、move_file 3、copy_file 2；stub_unknown 合计 0（非 0 行 0）；stub_failed 合计 14（非 0 行 14）
- orch-turn-none@d44：桩 303/1002 调用（30%）：run_shell_command 192、list_dir 50、edit_file 34、run_math 18、find_files 5、trash_file 2、run_applescript 1、rag_search 1；stub_unknown 合计 0（非 0 行 0）；stub_failed 合计 6（非 0 行 6）

orch-turn-none L0 失败 run 明细（7/20；why = 评分器原文，步数 = score.steps，元工具 = meta_calls 合计，桩 = stub_served 合计）：

- lc-hard-003 r0：步数超限: 13 > 11（步数 13，max_total_iterations 13，元工具 0，桩 2，aborted_reason「」）
- lc-hard-003 r1：步数超限: 12 > 11（步数 12，max_total_iterations 13，元工具 0，桩 1，aborted_reason「」）
- lc-hard-004 r0：步数超限: 13 > 11（步数 13，max_total_iterations 13，元工具 5，桩 4，aborted_reason「」）
- lc-hard-006 r0：files/merged.txt: exists 期望 True，实际 False；files/count.txt: exists 期望 True，实际 False（步数 7，max_total_iterations 11，元工具 0，桩 1，aborted_reason「」）
- lc-hard-006 r1：files/merged.txt: exists 期望 True，实际 False；files/count.txt: exists 期望 True，实际 False（步数 7，max_total_iterations 11，元工具 0，桩 1，aborted_reason「」）
- lc-hard-009 r0：步数超限: 11 > 10（步数 11，max_total_iterations 12，元工具 0，桩 4，aborted_reason「」）
- lc-hard-010 r0：步数超限: 14 > 9（步数 14，max_total_iterations 11，元工具 0，桩 8，aborted_reason「」）

### P. W1b：ceiling 压缩带回读句柄（事件 = guard_events_lines 切片；spill 文件 = 本地 sessions/<session>/spill/ 计数）

spill 文件含 W2 步骤边界压缩（spill_full）落的盘，不只 ceiling；事件里的 spilled 才是 ceiling 压缩给出句柄的条数。

| 组 | level | n | ceiling_fold 事件 | Σcompacted | Σspilled | fold 带 spill 的 run | context_hard 事件 | 超限中止（turn_abort context_guard）| 中止时量值 | spill 文件 | spill_readbacks（run 数） | 实发 ≥ ceiling 的生成 | 实发 > context_limit 的生成 | 实发最大 | recall (分母) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 0 | 20 | 0 | 0 | 0 | 0 | 0 | 0（compacted 0 / spilled 0） | — | 0 | 0（0） | 0 | 0 | 11,029 | — (0) |
| orch-turn-none | 49152 | 60 | 0 | 0 | 0 | 0 | 0 | 0（compacted 0 / spilled 0） | — | 0 | 0（0） | 101 | 0 | 56,455 | 100% (41) |
| orch-turn-none | 57344 | 60 | 0 | 0 | 0 | 0 | 0 | 0（compacted 0 / spilled 0） | — | 0 | 0（0） | 556 | 0 | 63,360 | 100% (41) |
| orch-turn-w1w2-rev | 0 | 20 | 0 | 0 | 0 | 0 | 0 | 0（compacted 0 / spilled 0） | — | 0 | 0（0） | 0 | 0 | 11,029 | — (0) |
| orch-turn-w1w2-rev | 49152 | 60 | 8 | 106 | 105 | 8 | 30 | 0（compacted 0 / spilled 0） | — | 1586 | 0（0） | 0 | 0 | 53,216 | 98% (43) |
| orch-turn-w1w2-rev | 57344 | 60 | 60 | 2116 | 2057 | 60 | 0 | 0（compacted 0 / spilled 0） | — | 2895 | 8（3） | 0 | 0 | 48,770 | 97% (39) |
| orch-turn-none@d44 | 0 | 20 | 0 | 0 | 0 | 0 | 0 | 0（compacted 0 / spilled 0） | — | 0 | 0（0） | 0 | 0 | 16,767 | — (0) |
| orch-turn-none@d44 | 32768 | 20 | 0 | 0 | 0 | 0 | 0 | 0（compacted 0 / spilled 0） | — | 0 | 0（0） | 0 | 0 | 36,669 | 83% (12) |
| orch-turn-none@d44 | 49152 | 20 | 0 | 0 | 0 | 0 | 0 | 0（compacted 0 / spilled 0） | — | 0 | 0（0） | 0 | 0 | 53,126 | 100% (11) |
| orch-turn-none@d44 | 57344 | 20 | 0 | 0 | 0 | 0 | 0 | 0（compacted 0 / spilled 0） | — | 0 | 0（0） | 206 | 0 | 61,581 | 100% (10) |

- 行级 `turn_guard_flags` 键：0/360 行带（不带时 ceiling_compacted / ceiling_spilled 只能从事件读，即上表）。
- 对照 orch-turn-none@d44 L57344：recall 100% (10)，成功率 35%

### Q. 前缀对照：本批无 orch-turn-full 臂（只有 orch-turn-none、orch-turn-w1w2-rev），orch-turn-w1w2-rev 对 orch-turn-full 不渲染

（本批无 catalog 臂（11-07 拍板删臂）：d44 批 catalog@d44 对 w1w2@d44 的 Q 表不并入。）

### U. analyze_d44 U 节（D43 (a) / D43b / D44 机制计数；本批只列本批 2 臂，不并跨批列）

#### U1. D43 (a)：49K / 57K 档回忆、来源与回读

桩·仅线索 列按当前 `compactor.CLUE_RULE`（值 `R2.1`）重建；规则一改此列复跑会变。

recall = analyze.aggregate 同口径（recall_ok 非空、未 aborted 的 run）；ITT = aborted 也进分母按未回忆计；来源 = 卡值计数（单位卡值，含 aborted run；analyze.recall_source）；「含来源 run」= 末轮正文里至少一个卡值归该来源的 run；桩·桩头 = 归桩的卡值落在桩补写 spill 原文首部 200 字（STUB_DIGEST_CHARS）里；桩·仅线索 = 不在首部、只在 compactor.observation_clue 重建的线索里命中——重建线索与运行时桩不保证一致（运行时桩不在 transcripts 里），这一列是重建口径；回读 = transcript 里 file_extract(path=spill:…) 调用；「读到卡」= journal 里该次回读结果（status=ok 且截断时补 spill 原文；非 ok 的回读不算读到）含任一卡值；journal 列 — = 至少一个 run 无会话目录或无 journal.jsonl；首迭代 fold = 本 run 有 iteration=1 的 context_guard_fold 事件（首次生成前就压）。

| 组 | level | n | aborted（迭代耗尽） | 首迭代 fold run | recall [Wilson] | recall ITT | 卡值来源 桩/回读/原文/? | 桩·桩头命中 | 桩·仅线索命中 | 含来源 run 桩/回读/原文 | 回读调用（run） | 每回读 run 回读 中位/最大 | journal 回读 / 读到卡 | 回读 run 中 aborted（迭代耗尽） |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| orch-turn-w1w2-rev | 49152 | 60 | 17（16） | 0 | 42/43 = 98% [88%,100%] | 42/60 = 70% [57%,80%] | 48/0/77/1 | 17 | 31 | 17/0/27 | 0（0） | —/0 | 0 / 0 | 0（0）/0 |
| orch-turn-w1w2-rev | 57344 | 60 | 21（21） | 60 | 38/39 = 97% [87%,100%] | 38/60 = 63% [51%,74%] | 82/1/30/1 | 16 | 66 | 37/1/23 | 8（3） | 1/6 | 8 / 3 | 2（2）/3 |

57K 档 recall_ok（未 aborted）run 按卡值来源分类：全桩（桩头）= 末轮正文里的卡值都归桩且都在桩头 200 字命中；全桩（含仅线索）= 都归桩、允许部分只在重建线索里命中（= 旧「全桩」口径）；含回读 = 至少一个归回读；含原文 / ? 同理（可重叠）：

- orch-turn-w1w2-rev：38 run；全桩（桩头）0、全桩（含仅线索）14、含回读 1、含原文 23、含 ? 1

全档（四档合计）回读调用 / 回读 run / 回读 run 中 aborted（迭代耗尽）：

- orch-turn-w1w2-rev：8 次 / 3 run / 2（2）；按档 run 数 0:0、49152:0、57344:3

#### U2. D43b：W2 步骤边界回读免压（step_readback_exempt 事件）

回读留存 = transcript 每次 spill 回读调用在最终 history 里的 feed 状态：原文 / 桩 / 拦截（去重、拒批、System Error，没真读；先于 ? 判定）/ ?（句柄不合法、无配对 feed、无 spill 原文或都不像；判据见 classify_readback docstring）。

| 组 | level | n | 有免压事件的 run | 免压事件数 | Σcount（w2_readback_exempt） | step_compaction 事件 | 回读 run | 回读留存 原文/桩/拦截/? |
|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 0 | 20 | 0 | 0 | 0 | 0 | 0 | 0/0/0/0 |
| orch-turn-none | 49152 | 60 | 0 | 0 | 0 | 0 | 0 | 0/0/0/0 |
| orch-turn-none | 57344 | 60 | 0 | 0 | 0 | 0 | 0 | 0/0/0/0 |
| orch-turn-w1w2-rev | 0 | 20 | 0 | 0 | 0 | 0 | 0 | 0/0/0/0 |
| orch-turn-w1w2-rev | 49152 | 60 | 0 | 0 | 0 | 44 | 0 | 0/0/0/0 |
| orch-turn-w1w2-rev | 57344 | 60 | 1 | 1 | 1 | 38 | 3 | 8/0/0/0 |

免压 run 逐条（本批全部 1 个；回读 = journal 里的 spill 回读；「卡值共现（含桩来源）」= 回读读到的卡值是否出现在之后的 step_completed 结果 / tool_started 参数 / 末轮正文——只是共现，值也可能来自桩，不证明是回读带出的；journal 回读 seq 缺失 = 不可定位、不判后续；「transcript feed 状态」= 该回读在 transcript feed（取自最终 history）里是原文还是已被改写成桩）：

- orch-turn-w1w2-rev L57344-lc-hard-006-r5-62a855（pass=False，recall_ok=False，aborted_reason「回合上限 11 耗尽仍在发调用」）：免压事件 step 1 count 1 level ceiling；step_compaction 边界 [1]；回读 1 次
  - seq 558 spill:42fc7ea7b8f8470d（原文 1755 字；transcript feed 状态 原文）：卡值共现（含桩来源） OPS-68989 → 无

#### U3. D44：实参预检回执

本批无 catalog 臂（11-07 拍板删臂）：U3 的 catalog − w1w2 按档表不渲染（前缀对照见 Q 节、V4）。

预检回执（feed 里 `System Error: '<名>' was not executed:`；类 = 回执首行里出现的子句；恢复 = O.missing_arg_recovered 同一 FIFO 规则按类记账；schema_fed 事件 = tool_error 事件 kind=precheck 且 schema_fed=true；附声明 = 回执含 `Full declaration of`；参数类 System Error / 恢复 = Q 节口径（O.syserr_class ≠ 其他的 System Error 回执数 / O.missing_arg_recovered），b2 的 TypeError 形态也算）：

| 组 | 回执（run） | 缺必填 / 未知键 / 两者兼有 / 其他 | 各类恢复 | 恢复率 | 附声明（回执文本） | schema_fed 事件 | precheck tool_error 事件 | full 尾句 | 按工具 | 参数类 System Error / 恢复（Q 节口径） |
|---|---|---|---|---|---|---|---|---|---|---|
| orch-turn-none | 5（5） | 0 / 0 / 5 / 0 | 缺 0 / 未 0 / 兼 5 | 100% | 5 | 5 | 5 | 0 | move_file 3、copy_file 2 | 5 / 5 |
| orch-turn-w1w2-rev | 3（3） | 0 / 0 / 3 / 0 | 缺 0 / 未 0 / 兼 3 | 100% | 3 | 3 | 3 | 0 | move_file 2、copy_file 1 | 3 / 3 |

被打回的调用是否计失败（本批全部含预检回执的 run）。分三层：

- harness 记账（同源字段，恒真，仅作一致性核对）：失败正则（O._FAILED_RESP_RE）对回执文本、行字段 system_errors、score.steps（longchain 评分器：function 调用总数）对 transcript tool_calls 总数——三者与回执过滤看的是同一份 feed / 调用，不独立于回执本身，命中只说明记账没漏，不是「编排层把打回当失败」的证据；
- 执行侧：journal 未执行 = 被拒调用在 journal 里没有同名同参的 tool_started（journal 与 transcript 分开落盘）；「只因计步失败」= why 只有步数超限、且 steps 减去回执数（再减紧随其后被 `Duplicate call blocked` 的同名同参重发数）≤ 上限，分两列给；score.steps 缺的 run 不判（记入「steps 缺」）；
- 编排层（spec F1：打回 = 失败调用 → unresolved_failure 置位 / 批次失败 → failure_swallow_nudge，原样重发 → duplicate_call）：每条回执按「同 run 同工具第 k 条回执 ↔ 第 k 个 kind=precheck 的 tool_error 事件」定位到事件流，看该事件之后（同 run 切片内）是否出现对应事件；`unresolved_failure` 是 orchestrator.py 的局部变量、不落事件流；某 kind 在本臂整个事件流里一次都没有 → 「—（事件流无此 kind）」而不是 0。编排层对打回调用的处理只有 golden test 证明（test_tool_arg_precheck.py：test_rejected_call_counts_as_failure_for_swallow_guard / test_identical_resend_of_rejected_call_is_blocked_as_duplicate，reviewer probe_swallow 为 oracle）；本节只核数据可见部分——failure_swallow_nudge 只在收尾时仍有未解失败才发，回执之后若有成功重发就不该出现，出现 0 次不是反证。

- orch-turn-none：回执 5 条 / 5 run；这些 run 通过 1/5
  - harness 记账（同源字段，恒真，仅作一致性核对）：命中失败正则 5/5；行 system_errors ≥ 本 run 回执数 5/5；score.steps = tool_calls 总数（打回的调用计步）5/5
  - 执行侧：被拒调用在 journal 无同名同参 tool_started 5/5（配回调用数 5 对回执 5）；只因计步失败（steps − 回执）0 run、（steps − 回执 − 同名同参重发拦截）0 run；同名同参重发被 `Duplicate call blocked` 0 条
  - 编排层（出现 / 回执数；回执定位到 precheck 事件 5/5）：unresolved_failure —（事件流无此 kind；局部变量不落事件）；failure_swallow_nudge —（事件流无此 kind）；duplicate_call（同工具）0/5，其中所在 run 有同名同参重发 0（重发 0 条）
  - 抽查 L0-lc-hard-003-r0-111336：被拒 [{"path": "duty-summary-tmp.txt", "destination": "duty-summary.txt"}]；同名调用序列 {"path": "duty-summary-tmp.txt", "destination": "duty-summar → {"source": "duty-summary-tmp.txt", "destination": "duty-summ；why「步数超限: 13 > 11」；steps 13 / tool_calls 13
  - 抽查 L0-lc-hard-010-r0-021274：被拒 [{"path": "blockers.txt", "destination": "~/notes/blockers.txt"}]；同名调用序列 {"path": "blockers.txt", "destination": "~/notes/blockers.tx → {"source": "blockers.txt", "destination": "~/notes/blockers.；why「步数超限: 14 > 9」；steps 14 / tool_calls 14
  - 抽查 L49152-lc-hard-003-r1-feefb3：被拒 [{"path": "duty-summary.tmp.txt", "new_path": "duty-summary.txt"}]；同名调用序列 {"path": "duty-summary.tmp.txt", "new_path": "duty-summary.t → {"source": "duty-summary.tmp.txt", "destination": "duty-summ；why「步数超限: 13 > 11」；steps 13 / tool_calls 13
- orch-turn-w1w2-rev：回执 3 条 / 3 run；这些 run 通过 0/3
  - harness 记账（同源字段，恒真，仅作一致性核对）：命中失败正则 3/3；行 system_errors ≥ 本 run 回执数 3/3；score.steps = tool_calls 总数（打回的调用计步）3/3
  - 执行侧：被拒调用在 journal 无同名同参 tool_started 3/3（配回调用数 3 对回执 3）；只因计步失败（steps − 回执）0 run、（steps − 回执 − 同名同参重发拦截）0 run；同名同参重发被 `Duplicate call blocked` 0 条
  - 编排层（出现 / 回执数；回执定位到 precheck 事件 3/3）：unresolved_failure —（事件流无此 kind；局部变量不落事件）；failure_swallow_nudge 0/3；duplicate_call（同工具）0/3，其中所在 run 有同名同参重发 0（重发 0 条）
  - 抽查 L0-lc-hard-003-r0-87caa1：被拒 [{"path": "duty-summary-tmp.txt", "destination": "duty-summary.txt"}]；同名调用序列 {"path": "duty-summary-tmp.txt", "destination": "duty-summar → {"source": "duty-summary-tmp.txt", "destination": "duty-summ；why「步数超限: 13 > 11」；steps 13 / tool_calls 13
  - 抽查 L0-lc-hard-010-r0-4fe98e：被拒 [{"path": "blockers.txt", "destination": "~/notes/blockers.txt"}]；同名调用序列 {"path": "blockers.txt", "destination": "~/notes/blockers.tx → {"source": "blockers.txt", "destination": "~/notes/blockers.；why「步数超限: 14 > 9」；steps 14 / tool_calls 14
  - 抽查 L49152-lc-hard-003-r1-60d036：被拒 [{"path": "duty-summary.tmp.txt", "new_path": "duty-summary.txt"}]；同名调用序列 {"path": "duty-summary.tmp.txt", "new_path": "duty-summary.t → {"source": "duty-summary.tmp.txt", "destination": "duty-summ；why「步数超限: 13 > 11」；steps 13 / tool_calls 13

#### U4. orch-turn-none L0 与 lc-hard-009

- orch-turn-none L0 成功率 13/20 = 65% [43%,82%]，Pass^2 5/10 = 50% [24%,76%]
  - lc-hard-009 L0：r0 pass=False（步数超限: 11 > 10）；r1 pass=True（—）
  - lc-hard-009 L49152：r0 pass=True（—）；r1 pass=True（—）；r2 pass=True（—）；r3 pass=False（步数超限: 15 > 10）；r4 pass=True（—）；r5 pass=True（—）
  - lc-hard-009 L57344：r0 pass=True（—）；r1 pass=True（—）；r2 pass=True（—）；r3 pass=True（—）；r4 pass=False（files/metrics/summary.json: exists 期望 True，实际 False）；r5 pass=True（—）

#### U5. 数据质量与口径对照（本批 vs b2，同名臂）

| 组 | 原始行 | 去重丢弃 | error 行 | 有效 run | 连贯（分母） | stamp 数 | 并发写入档 |
|---|---|---|---|---|---|---|---|
| orch-turn-none | 140 | 0 | 0 | 140 | 104/104 | 1 | — |
| orch-turn-w1w2-rev | 140 | 0 | 0 | 140 | 103/103 | 1 | — |

### V. P2 噪声基线专节（本节 = analyze_p2.py 新增；`@d44` = d44 批同名臂，`@b2` = b2 批同名臂）

#### V1. 逐字相同率（先看这个：判 vLLM 逐请求 seed 是否生效）

输出签名 = 每轮 (thoughts, text, tool_calls 含参数) + turn_response 的 JSON（排序键）逐字比较，按 (item, rep) 配对；gens 比 turns 多出的生成（回合上限耗尽时末次生成无对应 turn，transcripts 不存其文本）记 (generation_tokens, truncated, cancelled, repetition_guard) 进签名。首 prompt token 数 = 首次生成引擎回报 prompt_tokens + fill.prompt_tokens_measured（只比 token 数，transcripts 不存 prompt 原文）；system.json sha = 会话目录 system.json 的 sha256。

V1a. 跨臂（每格 = 输出逐字相同 / 配对数；填充档括号 = 首 prompt token 数相同的对数）：

| 臂 a | 臂 b | L0 输出相同 | L0 首 prompt token 数相同 | L0 system.json sha 相同 | 32768 | 49152 | 57344 |
|---|---|---|---|---|---|---|---|
| none | w1w2-rev | 20/20 = 100% | 20/20 | 20/20 | — | 27/60（60） | 0/60（0） |

- system.json 只存 system 消息（role / content），不含工具声明：sha 相同只说明 system prompt 相同，分不出 full 与 catalog 前缀（none–full 也全同）。本批无 catalog 臂（11-07 拍板删臂）；prod 臂生效前缀看行内 prefix_decl / tool_schema_mode_effective（V4 末行）。
- gens 比 turns 多的行（签名里只记其 token 数与标记）：全 2 臂合计 7，按档 0:0、32768:0、49152:2、57344:5
- gens 比 turns 少的行（turns 全部进签名，不受影响）：0
- 全 2 臂 L0 输出逐字相同：20/20
- L0 逐字 100% 相同的类：{none, w1w2-rev}（类内 L0 差恒为 0、CI 零宽，不是零效应证据；独立的 L0 样本只有 1 份）
- 32768 档逐字 100% 相同的类：无
- 49152 档逐字 100% 相同的类：无
- 57344 档逐字 100% 相同的类：无
- L0 首 prompt token 数逐 (item, rep) 全同的类：{none, w1w2-rev}；L0 首 prompt 中位：none 6,194、w1w2-rev 6,194
- 全档逐字相同的臂对：无

V1b. 同臂内不同 rep 之间（同题 rep 两两比；应 ≈ 0%，否则 rep 没换种子）：

| 臂 | 0 | 32768 | 49152 | 57344 |
|---|---|---|---|---|
| none | 0/10 | — | 0/150 | 0/150 |
| w1w2-rev | 0/10 | — | 0/150 | 0/150 |

V1c. 种子核对（seed_check）：行 sampling_seed ≠ seed×1000+rep 或 sampling_seed_sent 非真的行 0 条；每个 rep 跨臂跨档的种子取值集合大小 r0:1、r1:1、r2:1、r3:1、r4:1、r5:1（1 = 共同随机数成立）。sampling_seed_applied 是进程内 MLX 播种字段，远端恒 False，不用。

V1d. 分叉对符号检验：只在输出分叉的 (题, rep) 对上数 b 过 a 不过（b 赢）/ a 过 b 不过（a 赢），同过同败不计；双侧二项 p（A.sign_test_p）。(题, rep) 对按独立计，同题多 rep 相关未校正。部分逐字相同的臂对（如 w1–w1w2）在未分叉的对上差恒 0，V2 的拆分零分布按「全部 (题, rep) 都独立抽样」构造，用在这类臂对上偏保守，本表是补充。

| 臂 a | 臂 b | level | 分叉对 | b 赢 / a 赢 | p |
|---|---|---|---|---|---|
| none | w1w2-rev | 49152 | 33 | 3 / 1 | 0.62 |
| none | w1w2-rev | 57344 | 60 | 12 / 14 | 0.85 |
| none | w1w2-rev | **全档** | 93 | 15 / 15 | 1.00 |

#### V2. L0 噪声基线（臂内 2 rep；旧「三臂 L0 差 15 点」口径作废）

V2a. 逐题通过数 / rep 数（L0）：

| 题 | none | w1w2-rev |
|---|---|---|
| lc-hard-001 | 2/2 | 2/2 |
| lc-hard-002 | 2/2 | 2/2 |
| lc-hard-003 | 0/2 | 0/2 |
| lc-hard-004 | 1/2 | 1/2 |
| lc-hard-005 | 2/2 | 2/2 |
| lc-hard-006 | 0/2 | 0/2 |
| lc-hard-007 | 2/2 | 2/2 |
| lc-hard-008 | 2/2 | 2/2 |
| lc-hard-009 | 1/2 | 1/2 |
| lc-hard-010 | 1/2 | 1/2 |

V2b. 臂级 L0：各 rep 通过率（10 题）、rep 间标准差、合计与 bootstrap 95% CI（种子 20261031、10000 次）。成功率给两种区间：按题聚类（只按题重抽，题内 rep 原样）与两段（再题内按 rep 重抽，rep 方差重复计入、偏宽，只作对照）；Pass^2 只按题重抽：

| 臂 | 各 rep 通过率 | 均值 | rep 间 SD | 成功率 k/n | 成功率 CI 按题 | 宽 | 成功率 CI 两段 | 宽 | Pass^2（无偏） | Pass^2 CI | 宽 | Pass@2 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| none | 50% / 80% | 65.0 | 21.2 点 | 13/20 | [40,90] | 50 | [40,90] | 50 | 5/10 = 50.0 | [20,80] | 60 | 80.0 |
| w1w2-rev | 50% / 80% | 65.0 | 21.2 点 | 13/20 | [40,90] | 50 | [40,90] | 50 | 5/10 = 50.0 | [20,80] | 60 | 80.0 |

V2c. 拆分零分布：从同一臂 L0 的 2 rep 里取两组不相交 rep 当两条假臂（每条 = 10 题 × m rep；m=2（本批 32K/49K/57K 档实际 6 rep、—，不是 2 rep 设计——按在场臂数与 rep 数现算）），枚举全部无序拆分得 |Δ| 的零分布。只用 L0 逐字相同类的代表（none），逐字相同的臂不重复计。**门槛 t = 零分布可取值里 P(|Δ| ≥ t) ≤ 5% 的最小值**（零分布离散，直接按枚举计尾概率，不用分位数）。拆分之间共用 rep，尾概率是近似；本批 L0 通过率 none 65%；填充档的题级通过率分布可能与 L0 不同（本节未逐档核算），用到通过率近 0 / 100% 的档时门槛偏保守。rep 数 < 2m 的代表拆不出零分布（拆分数 0）。

| 零分布 | m | 拆分数 | |Δ| 中位 | 最大 | 门槛 t（P(|Δ|≥t)） | 尾概率表 v:P(|Δ|≥v) |
|---|---|---|---|---|---|---|
| 成功率 · none | 2 | 0 | — | — | > 最大 |  |
| **成功率 · 合并** | 2 | 0 | — | — | **> 最大** |  |
| 成功率 · none | 3 | 0 | — | — | > 最大 |  |
| **成功率 · 合并** | 3 | 0 | — | — | **> 最大** |  |
| Pass^2 · none | 2 | 0 | — | — | > 最大 |  |
| **Pass^2 · 合并** | 2 | 0 | — | — | **> 最大** |  |
| Pass^2 · none | 3 | 0 | — | — | > 最大 |  |
| **Pass^2 · 合并** | 3 | 0 | — | — | **> 最大** |  |

V2d. 解析近似值（对照）：s = 等价类代表 rep 间 SD 的方差均值开方 = 21.2 点（由 none 21.2 合并，s 本身不确定性大）；两条独立臂各 m rep 的成功率差 SD ≈ s·√(2/m)，1.96 倍：m=2 → 41.6 点，m=6 → 24.0 点。只含 rep 间噪声。

**建议门槛**：同档两臂（10 题 × 2 rep，m=2 零分布）成功率差 > 零分布最大值、Pass^2 差 > 零分布最大值 才算超出 L0 rep 噪声（P(|Δ| ≥ t) ≤ 5%）；L0 两臂各 2 rep 时以 m=3 拆分值作参考（成功率 > 零分布最大值、Pass^2 > 零分布最大值；2 rep 对 2 rep 的真实噪声更小，m=3 值偏保守）。逐字相同的臂对不适用（差恒 0）。下表凡「超噪声 / 噪声内」都按此判，并给观察差的尾概率。

#### V3. 2 臂 × 四档与 d44 / b2 同臂同档（成功率 k/n；Pass^2 = Σ逐题无偏 / 题数；L0 本批 2 rep）

口径差异：本批 prod 臂（none/w1/w1w2）生效前缀 = catalog（D44 (iii) c76492f），d44 / b2 的 prod 生效 = full；所以 d44 / b2 的 w1w2 与本批 **full** 臂同形态（guard on / step on / full 前缀），表里「同形态」列按此对齐。d44 / b2 无 seed（sampling_seed_sent 缺键），本批 seed 透传、臂间共同随机数。b2 → d44 → 本批之间另有生产代码改动（见方案 §6），跨批差不只归前缀。L0「前 2 rep」= 本批 rep 0/1，与旧批 n=2 设计对齐。

| 本批臂 | level | 本批 成功率 | 本批 Pass^2 | Pass@2 | L0 前 2 rep 成功率 / Pass^2 | d44 同名 成功率 / Pass^2 | b2 同名 成功率 / Pass^2 | 同形态 d44 / b2 臂 | 同形态 d44 成功率 / Pass^2 | 同形态 b2 成功率 / Pass^2 |
|---|---|---|---|---|---|---|---|---|---|---|
| none | 0 | 13/20 = 65% | 5/10 = 50% | 80% | 13/20 / 5/10 | 7/20 / 2/10 | 12/20 / 5/10 | none | 7/20 / 2/10 | 12/20 / 5/10 |
| none | 49152 | 27/60 = 45% | 2.27/10 = 23% | 67% | — | 6/20 / 1/10 | 4/20 / 1/10 | none | 6/20 / 1/10 | 4/20 / 1/10 |
| none | 57344 | 26/60 = 43% | 2.27/10 = 23% | 64% | — | 7/20 / 2/10 | 3/20 / 1/10 | none | 7/20 / 2/10 | 3/20 / 1/10 |
| w1w2-rev | 0 | 13/20 = 65% | 5/10 = 50% | 80% | 13/20 / 5/10 | — | — | w1w2-rev | — | — |
| w1w2-rev | 49152 | 29/60 = 48% | 2.67/10 = 27% | 70% | — | — | — | w1w2-rev | — | — |
| w1w2-rev | 57344 | 24/60 = 40% | 1.80/10 = 18% | 62% | — | — | — | w1w2-rev | — | — |

V3b. 本批 − d44 同形态臂（共同题集点差，A.common_diff；L0 取本批前 2 rep，与 d44 的 2 rep 对齐；括号 = m=2 零分布里 P(|Δ| ≥ |观察差|) 与按 V2 门槛的判定）：

| 本批臂 | d44 臂 | level | 成功率差 | Pass^2 差 |
|---|---|---|---|---|
| none | none@d44 | 0 | +30（P=—，超出支撑） | +30（P=—，超出支撑） |
| none | none@d44 | 49152 | +15（P=—，超出支撑） | +13（P=—，超出支撑） |
| none | none@d44 | 57344 | +8（P=—，超出支撑） | +3（P=—，超出支撑） |

拐点（analyze.knee：metric Pass^2、基线候选 [0, 4096]、drop 0.20、recover 0.10；本批 L0 基线 2 rep，其余档 6 rep）：

- orch-turn-none：拐点 49152（基线 L0 = 50%）
- orch-turn-w1w2-rev：拐点 49152（基线 L0 = 50%）
- orch-turn-none@d44：基线 20% ≤ drop 20%：基线在地板，规则空判（到 57344 未判）（基线 L0 = 20%）
- orch-turn-none@b2：拐点 32768（基线 L0 = 50%）

#### V4. 本批无 orch-turn-full 臂：改列 w1w2-rev − none（b − a，按题配对；区间只按题聚类重抽，种子 20261031）

区间 = paired_item（只按题重抽、题内 rep 原样）；analyze.paired_units 自 2d3fd99c 起成功率也只按题重抽（同结构、同 RNG 消耗），E / Q / U3 节区间与本节同口径（p2 档案里的两段重抽宽区间不再复现）。尾概率 = V2c 零分布 P(|Δ| ≥ |观察差|)：两侧都 ≥ 6 rep 的格（L0 6 rep 对 6 rep）用 m=3 拆分值（偏保守），其余用 m=2。两侧 rep 编号集合不同的格（如 L0 6 rep 对 2 rep）两侧都截到共同 rep 再配对（共同随机数下 rep r 只对 rep r），格内注明「rep 0–1 配对」与截前→截后 rep 数，a → b 列也是截后值。

| 对比 | level | 成功率 a → b | 成功率差（尾概率，判定） | 按题 95% CI（题升/降） | Pass^2 a → b | Pass^2 差（尾概率，判定） | 按题 95% CI（题升/降） |
|---|---|---|---|---|---|---|---|
| w1w2-rev − none | 0（m=2） | 65% → 65% | +0（P=—，超出支撑） | +0.0 [+0.0,+0.0]（0↑/0↓） | 50% → 50% | +0（P=—，超出支撑） | +0.0 [+0.0,+0.0]（0↑/0↓） |
| w1w2-rev − none | 49152（m=3） | 45% → 48% | +3（P=—，超出支撑）；m=2 P=— | +3.3 [+0.0,+8.3]（2↑/0↓） | 23% → 27% | +4（P=—，超出支撑）；m=2 P=— | +4.0 [+0.0,+10.7]（2↑/0↓） |
| w1w2-rev − none | 57344（m=3） | 43% → 40% | -3（P=—，超出支撑）；m=2 P=— | -3.3 [-20.0,+13.3]（4↑/5↓） | 23% → 18% | -5（P=—，超出支撑）；m=2 P=— | -4.7 [-22.7,+12.0]（4↑/4↓） |
| w1w2-rev − none | 四档合并 | — | -0（合并无零分布） | -0.0 [-5.0,+5.0]（4↑/4↓） | — | -0 | -0.2 [-4.7,+4.4]（5↑/4↓） |
| w1w2-rev − none | 三填充档合并 | — | -0（合并无零分布） | -0.0 [-7.5,+7.5]（4↑/4↓） | — | -0 | -0.3 [-7.0,+6.7]（5↑/4↓） |

首 prompt 中位（L0 / 57344）：none 6,194 / 57,554、w1w2-rev 6,194 / 41,104
前缀元数据（行内字段；prefix_decl 逐行 == tool_schema_mode_effective，前缀元数据随生效档位（2d3fd99c））：none prefix_mode=['prod'] prefix_decl=['catalog'] prefix_n_tools(prompt, prompt_full)=[('61', '17'), ('64', '20')] tool_schema_mode_effective=['catalog'] prefix_base_tokens=[6051] agent_cfg_fingerprint=['bd006ebc49cf403b']；w1w2-rev prefix_mode=['prod'] prefix_decl=['catalog'] prefix_n_tools(prompt, prompt_full)=[('61', '17'), ('64', '20')] tool_schema_mode_effective=['catalog'] prefix_base_tokens=[6051] agent_cfg_fingerprint=['7b2fd89bae0acfcb']

#### V5. 时长（run_colab.log 臂起止；Σwall = transcripts 行 wall_s 之和）

| 臂 | 本批墙钟 min | d44 同形态臂 | d44 墙钟 min | 墙钟增量 min | 本批 L0 rep 数 | 本批 Σwall L0 min | 本批 Σwall 填充三档 min | L0 多出的 rep 估计 min（= L0 Σwall × (rep−2)/rep） | d44 Σwall L0 / 填充三档 min |
|---|---|---|---|---|---|---|---|---|---|
| none | 204.2 | none | 91.1 | +113.1 | 2 | 8.1 | 194.6 | 0.0 | 10.6 / 79.7 |
| w1w2-rev | 192.2 | w1w2-rev | — | — | 2 | 8.2 | 183.2 | 0.0 | — |

- 每 run wall_s 中位（s，按档 0/32768/49152/57344）：none 25.3/—/100.1/86.3；w1w2-rev 25.7/—/99.7/87.1
- 本批 2 臂墙钟合计 396.4 min（6.61 h）；d44 四臂合计 354.3 min。
- 本批 run_colab.log 首行 → DONE：417.0 min（6.95 h）；READY 时刻 ['2026-11-09 08:50:32']；`START deploy` 2 次（> 1 = 首次起机未 READY 后按 READY_RETRY 重试）。
- d44 run_colab.log 首行 → DONE：367.6 min（6.13 h）；`START deploy` 1 次。

### W. 本批对 --p2 批（同 KNEE_SEED、同 per-request 种子；`@p2` = w12eval 批同名臂；本节 = analyze_p2.py 新增）

共同臂：none；p2 有、本批无：w1w2（不进本节）。配对键 = (item, rep)，同档。

#### W1. 跨批逐字相同与输入差（先看这个：输出大多相同 = 改动没碰到模型输入或没改变输出）

每格 = 相同对 / 配对数（配对 = 两批都有的 (item, rep)；「仅 w12eval / 仅本批」= 只在一侧的行数，不进配对）。首 prompt 差 = d48 − w12eval 的首次生成 prompt_tokens（引擎回报）取值:对数；填充档输入签名另含 fill.prompt_tokens_measured。首个分叉轮 = 第一处 (thoughts, text, tool_calls) 不同的轮下标:对数。

| 臂 | level | 仅 w12eval / 仅本批 行数 | 输出相同 | 输入签名相同 | system.json sha 相同 | 填充 fingerprint 相同 | 首 prompt 差:对数 | prefix_base_tokens w12eval → d48 | 首个分叉轮:对数 |
|---|---|---|---|---|---|---|---|---|---|
| none | 0 | 40 / 0 | 1/20 | 20/20 | 20/20 | 20/20 | +0:20 | 6051 → 6051 | t0:8、t1:6、t2:1、t3:3、t4:1 |
| none | 32768 | 60 / 0 | 0/0 | 0/0 | 0/0 | 0/0 | — | 6051 → 6051 | — |
| none | 49152 | 0 / 0 | 1/60 | 60/60 | 60/60 | 60/60 | +0:60 | 6051 → 6051 | t0:57、t2:1、t3:1 |
| none | 57344 | 0 / 0 | 0/60 | 60/60 | 60/60 | 60/60 | +0:60 | 6051 → 6051 | t0:49、t1:3、t2:5、t3:2、t4:1 |

- 按档合计输出相同：0:1/20、49152:1/60、57344:0/60
- prefix_base_tokens 差（d48 − w12eval，只取两批行内前缀元数据都按生效档位计的臂，prefix_meta_ok）：[0]；全部配对的首 prompt 差：+0:140；其中填充 fingerprint 相同的对：+0:140
- 填充 fingerprint 不同的对 0：无（这些对的填充内容不同，跨批输入不只差前缀 / 系统提示；原因本脚本未核验。W2 / W3 按臂档标出其个数）
- 判读：填充相同的对上首 prompt 差恒等于 prefix_base_tokens 差 +0——两批输入的 token 数差只来自前缀这一项（system.json 文本差见下），填充与任务部分不变；L0 输出逐字相同 1/20：同种子下输出已分叉（首个分叉轮见表末列；改动是否有效看 W2 与步数 / 回读证据，分叉本身不是证据）。
- system.json content 逐行差：1 种不同的差（覆盖 140 对；缺 system.json 的对 0）。各种差按出现对数：
  - 140 对（0 行）：

#### W2. 同臂同档 d48 − w12eval（按题配对；区间 = paired_item，只按题重抽，种子 20261031；尾概率 = V2c 本批零分布，两侧都 ≥ 6 rep 的格用 m=3、其余用 m=2。两批 rep 编号集合不同的格（如某臂某档：w12eval rep 数更多、d48 rep 数更少）两侧都截到共同 rep 再配对，格内注明「rep 0–1 配对」与截前→截后 rep 数，w12eval → d48 列与 recall 也是截后值）

| 臂 | level | 成功率 w12eval → d48 | 差（尾概率，判定） | 按题 95% CI（升/降） | Pass^2 w12eval → d48 | 差（尾概率，判定） | 按题 95% CI（升/降） | recall w12eval → d48 | recall 差 按题 95% CI（升/降） |
|---|---|---|---|---|---|---|---|---|---|
| none | 0（rep 0–1 配对，none@p2 6→2 rep） | 12/20 = 60% → 13/20 = 65% | +5（P=—，超出支撑） | +5.0 [-15.0,+25.0]（3↑/2↓） | 4/10 = 40% → 5/10 = 50% | +10（P=—，超出支撑） | +10.0 [-30.0,+50.0]（3↑/2↓） | — → — | — |
| none | 49152 | 30/60 = 50% → 27/60 = 45% | -5（P=—，超出支撑） | -5.0 [-16.7,+6.7]（3↑/3↓） | 3/10 = 30% → 2.27/10 = 23% | -7（P=—，超出支撑） | -7.3 [-22.7,+7.3]（2↑/3↓） | 42/42 → 41/41 | +0.0 [+0.0,+0.0]（0↑/0↓） |
| none | 57344 | 27/60 = 45% → 26/60 = 43% | -2（P=—，超出支撑） | -1.7 [-15.0,+8.3]（3↑/2↓） | 2.60/10 = 26% → 2.27/10 = 23% | -3（P=—，超出支撑） | -3.3 [-14.0,+4.7]（3↑/2↓） | 41/41 → 41/41 | +0.0 [+0.0,+0.0]（0↑/0↓） |
| none | 四档合并（0 档 rep 0–1 配对） | — | -1（合并无零分布） | -0.6 [-7.8,+6.1]（4↑/3↓） | — | -0 | -0.2 [-13.8,+12.9]（5↑/3↓） | — | — |
| none | 三填充档合并 | — | -3（合并无零分布） | -3.3 [-10.0,+2.5]（3↑/4↓） | — | -5 | -5.3 [-13.0,+1.7]（3↑/4↓） | — | — |

#### W3. 跨批分叉对符号检验（a = w12eval，b = d48；只数输出分叉的 (题, rep) 对，同题多 rep 相关未校正；配对键 (item, rep)，只在一侧的 rep 自然不进；「填充 fp 不同」= 该臂档填充 fingerprint 跨批不同的配对数，不论是否分叉，见 W1）

| 臂 | level | 分叉对 | d48 赢 / w12eval 赢 | p | 填充 fp 不同 |
|---|---|---|---|---|---|
| none | 0 | 19 | 3 / 2 | 1.00 | 0 |
| none | 49152 | 59 | 7 / 10 | 0.63 | 0 |
| none | 57344 | 60 | 9 / 10 | 1.00 | 0 |
| none | **全档** | 138 | 19 / 22 | 0.76 | 0 |

#### W4. 曲线：本批 / p2 / d44 / b2（同形态臂，FORM_MATCH）/ 参照 qwen-current（直调）

重复护栏率 = repetition_runs / n（score.repetition_hits > 0 的 run）。recall = analyze.aggregate 口径（recall_ok 非空、未 aborted）。成功率带拐点 = 首个成功率 < 本组 L0 成功率按题聚类 95% CI 下界的填充档。

W4 · 成功率：

| 组 | 0 | 32768 | 49152 | 57344 |
|---|---|---|---|---|
| orch-turn-none | 13/20 = 65% | — | 27/60 = 45% | 26/60 = 43% |
| orch-turn-none@p2 | 44/60 = 73% | 23/60 = 38% | 30/60 = 50% | 27/60 = 45% |
| orch-turn-none@d44 | 7/20 = 35% | 6/20 = 30% | 6/20 = 30% | 7/20 = 35% |
| orch-turn-none@b2 | 12/20 = 60% | 6/20 = 30% | 4/20 = 20% | 3/20 = 15% |
| orch-turn-w1w2-rev | 13/20 = 65% | — | 29/60 = 48% | 24/60 = 40% |
| qwen-current | 18/20 = 90% | 18/20 = 90% | 18/20 = 90% | — |

W4 · Pass^2：

| 组 | 0 | 32768 | 49152 | 57344 |
|---|---|---|---|---|
| orch-turn-none | 5/10 = 50% | — | 2.27/10 = 23% | 2.27/10 = 23% |
| orch-turn-none@p2 | 6/10 = 60% | 2/10 = 20% | 3/10 = 30% | 2.60/10 = 26% |
| orch-turn-none@d44 | 2/10 = 20% | 1/10 = 10% | 1/10 = 10% | 2/10 = 20% |
| orch-turn-none@b2 | 5/10 = 50% | 2/10 = 20% | 1/10 = 10% | 1/10 = 10% |
| orch-turn-w1w2-rev | 5/10 = 50% | — | 2.67/10 = 27% | 1.80/10 = 18% |
| qwen-current | 9/10 = 90% | 9/10 = 90% | 9/10 = 90% | — |

W4 · recall：

| 组 | 0 | 32768 | 49152 | 57344 |
|---|---|---|---|---|
| orch-turn-none | — | — | 41/41 | 41/41 |
| orch-turn-none@p2 | — | 35/35 | 42/42 | 41/41 |
| orch-turn-none@d44 | — | 10/12 | 11/11 | 10/10 |
| orch-turn-none@b2 | — | 11/13 | 12/13 | 9/10 |
| orch-turn-w1w2-rev | — | — | 42/43 | 38/39 |
| qwen-current | — | 20/20 | 20/20 | — |

W4 · 重复护栏率：

| 组 | 0 | 32768 | 49152 | 57344 |
|---|---|---|---|---|
| orch-turn-none | 0/20 | — | 1/60 | 3/60 |
| orch-turn-none@p2 | 0/60 | 0/60 | 0/60 | 1/60 |
| orch-turn-none@d44 | 0/20 | 0/20 | 1/20 | 0/20 |
| orch-turn-none@b2 | 0/20 | 0/20 | 0/20 | 1/20 |
| orch-turn-w1w2-rev | 0/20 | — | 1/60 | 2/60 |
| qwen-current | 0/20 | 0/20 | 0/20 | — |

拐点（analyze.knee：Pass^2、基线 [0, 4096]、drop 0.20、recover 0.10）与成功率带拐点：

- orch-turn-none：拐点 49152；成功率带拐点：L0 CI 下界 40% → 无填充档跌破
- orch-turn-none@p2：拐点 32768；成功率带拐点：L0 CI 下界 55% → 首个跌破 32768
- orch-turn-w1w2-rev：拐点 49152；成功率带拐点：L0 CI 下界 40% → 无填充档跌破

<!-- END AUTO -->
