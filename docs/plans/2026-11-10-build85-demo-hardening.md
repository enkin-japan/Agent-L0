# build 85：展示前加固（熔断可见 / 强制作答 / 搜索不可用快速失败 / 搜索 API 提供方）

日期：2026-11-10。背景：11-11 中途发表要在笔记本上用 macOS app 现场展示（日语任务、5 分钟内、
联网查实时信息）。build 84 彩排时一道新闻题「卡死后无答复」，用户令诊断并修。

## 1. 诊断（主会话 + analyst，证据只取用户授权的那一个失败会话与对应 backend.log 行）

- 不是 build 84 的回归，build 83 同机制。一次 `web_research` 的子沙箱最多 6 步 + 1 次强制摘要
  = 7 次子生成；失败轮调了 12 次 = 84 ≥ `max_sub_generations` 80 → 子调研熔断。
- 熔断说明只放在 `run_turn` 返回值的 `response` 里，而 `turn_runner` 落盘 / 下发只取
  `session.persistable_messages(...)` → 用户看到的是「没有答复」。
- 编排层写给模型的 `[System guardrail] …`（role=system）随轮次下发，被 app 当成聊天气泡显示。
- 真因在上游：内置 SearXNG 的抓取型引擎（brave / duckduckgo / startpage 等）按出口 IP 限流，
  彩排把额度烧光；被暂停的引擎在**每次**查询的 `unresponsive_engines` 里都会出现（CAPTCHA
  暂停 ≥ 1 h）。模型拿不到结果就反复调研直到熔断。

## 2. 用户决定（2026-11-10，逐条）

1. 修四件：「熔断原因显示出来、隐藏那个英文气泡、引擎全挂时快速失败、预算将尽时强制作答」。
2. 不新增「抓指定网页」工具——curl 弹审批主要是提示语点名网站所致；展示 / 彩排提示语按普通人
   说话方式写（不点名网站、网址、API）。审批卡对 curl 时弹时不弹的不一致，停止追查。
3. 搜索架构：设置里新增一页添加网络搜索 API；**API 优先，现有 SearXNG 兜底**；多个 API 随机
   排序，但凡有一个可用 API 就走 API。首批 Brave、Tavily。
4. Google Search grounding 不做：它是一次 Gemini 模型调用而非搜索接口，条款（2026-03-23 生效）
   禁止程序化抽取链接 / 拿链接去抓取、要求原样展示 Search Suggestions、不得缓存，与本项目
   「搜索 → 候选 → 读页 → 摘要」管线冲突。
5. 密钥不走对话：用户在 app 设置页自己填，主会话与子代理都不接触密钥值。

## 3. 设计

### A. 熔断可见 + 强制作答（`backend/framework/core/orchestrator.py`、app `ChatMessage.swift`）

- A1：子调研熔断、总迭代熔断、runaway_call、parse_loop 四个出口的说明文字用
  `session.add_assistant_message` 落进会话。
- A2 强制作答：循环顶判两条触发——T1 `max_sub_generations − 已用 < forced_answer_sub_reserve`
  （代码缺省 7 = 一次调研的上限；要求已用 > 0），T2 主循环只剩最后一次迭代（要求已迭代 > 0）；
  检索证据未决（`retrieval.unresolved`）时不判。触发后做**一次**不给工具的生成（`tools=[]`、
  `grammar_tools=[]`），尾注 `FORCED_ANSWER_NOTE`（不落盘；最近工具失败未解时加
  `FORCED_ANSWER_FAILURE_NOTE`），`max_tokens` 升到 `min(ESCALATED_MAX_TOKENS, reserve)`。
  有正文且无工具调用、无复读 → 走与正常收尾共用的 `_finalize_answer`，状态由共用的
  `_answer_status` 定（未解失败或零变更却声称做了动作 → warning），`metrics.forced_answer`
  记原因、发 `turn_forced_answer` 事件；否则产出丢弃，落原熔断出口。
- 开关 `forced_answer`（代码缺省开，config.yaml 不设键）：`benchmarks/summary_trace/run_trace.py`
  拿 `max_total_iterations` 当截断点，显式关。`run_knee` 未动，留给 D48c / D49 那一片加
  `--forced-answer` 开关。
- A3：app 不为以 `[System guardrail]` 开头的 system 消息生成气泡，也不进会话内搜索
  （`ModelFacingMessage`）；其余 system 消息照常。

### B. 搜索不可用快速失败（`agents/web_tools.py`、`agents/research_sandbox.py`、`mcp/tools/web_research.py`）

- 判据：请求失败（超时 / 连接 / 非 2xx / 非法 JSON）即不可用；「0 结果且有引擎不响应」不能直接
  判——被暂停的引擎每次都在——而是发一条确认查询（`SEARCH_CONFIRM_QUERY`）：有结果 = 原查询
  确实无结果（记健康时间戳，TTL 内不再确认），仍无结果或失败 = 不可用。
- 不可用 → 返回 `SEARCH_UNAVAILABLE:` 标记并置进程级闩（`search_unavailable_ttl` 60 s，线程安全，
  成功即清）。子沙箱在「不可用 ∧ 候选池空 ∧ 没打开过有效页」时提前终止，返回 System Error
  （叫模型本轮别再调研、用户给了网址就用 web_read）；`web_research` 入口在闩住且查询里没有
  http(s) 网址时直接短路。
- 顺带：`_ssrf_ok` 拦 100.64.0.0/10（含 IPv4-mapped），地址解析不了按拒绝处理。

### C. 搜索 API 提供方（`agents/search_providers.py`、`api/routes.py`、`api/schemas.py`、app 设置页「搜索」）

- 非密配置 `<data_root>/search_providers.json`（0600、原子替换）；密钥走既有
  `read_secret` / `write_secret`，名 `SEARCH_API_KEY_<ID>`（`api_tool_factory.py` 未改）。
- 搜索链：启用 ∧ 有密钥 ∧ 未在冷却的 API 随机排序，第一个成功即用（合法 0 结果不下落）；
  API 全不可用 → SearXNG（B 的判据移到这一层）；整条链不可用 → `SEARCH_UNAVAILABLE`。
- 冷却按提供方记：一般失败 60 s、鉴权 600 s、额度 3600 s，`Retry-After` 尊重但封顶 600 s。
- 一审修补：API 请求超时单列 8 s（`search_api_timeout`）、不跟随重定向（Brave 的自定义密钥头跨站重定向时
  不会被 requests 剥掉）；密钥 / 标签拒收一切行界字符（含 C1 与 U+2028 / U+2029，密钥文件按
  `splitlines()` 切行）；删除先清钥再落列表；PATCH 先落列表再写钥；损坏的配置文件在被覆盖前改名留底。
- 提供方：Brave（`GET api.search.brave.com/res/v1/web/search`，头 `X-Subscription-Token`）、
  Tavily（`POST api.tavily.com/search`，`Authorization: Bearer`）。文档 2026-11-10 核对；Brave 的
  错误码与限流头文档里没查到，按通用 HTTP 语义处理。
- 接口见 PROTOCOL.md §2.12；响应从不含密钥。工具注册条件 = SearXNG 在 ∨ 配了任一 API。

## 4. 验证记录

审查两道门、变异证明与测试结果见本批提交说明。变异清单与执行器入库在 `benchmarks/build85_mutations/`
（`run_mutants.py` + `specA.json` / `specB.json` / `specC.json`）。

## 5. 残留（另记 DEFERRED.md §10）

熔断 / 强制作答的状态文字是写死的中文；密钥删除留空行；后加 API 不立即上架；API 不做额度计量；定时任务也会消耗付费 API；
`web_research` 仍可被模型构造的网址带出数据；curl 审批卡不一致未解释；Google 若要接，
可选 Custom Search JSON API 而非 grounding。

## 6. 发布后彩排与阶段复审（2026-11-10 傍晚，Mac mini，MacAgent build 85 公证版）

### 6.1 现场
- Colab：16:45 首次申请 A100 120 s 超时未分到（服务端无 VM、无扣费，`stop --confirm-unallocated` 清本地租约）；16:48 重起，17:02 READY；17:57 停机并经服务端确认释放（余额 463.47 → 456.21 单位）。
- 搜索 API：用户在设置「検索」页自己填了 Brave Search 与 Tavily，17:02 点「テスト」各返回 8 件、状态「正常」（首次用真钥验证）。

### 6.2 彩排记录（日语自然语言提示语，不点名网站 / 接口）
| 题 | 次 | 执行过程 | 搜索 | curl | 审批卡 | 结果 |
|---|---|---|---|---|---|---|
| 天气（東京・大阪・福岡） | 1 | 13 步 106 s | 1 次正常（走 API） | 3（Open-Meteo） | 0 | 折线图 + 推荐大阪周六 |
| 旅行综合（ソウル・台北・バンコク：天气 + 汇率 + 图 + 换算表 + 推荐） | 1 | 18 步 99 s | 2 次正常 | 0 | 0 | 推荐台北，Sources 16 条 |
| 同上 | 2 | 19 步 224 s | 3 次正常 | 0 | 0 | 推荐台北 |
| 同上 | 3 | 墙钟 7 分 45 秒（大半是等人点卡） | **首调即 SEARCH_UNAVAILABLE** | 3 | 3 | 推荐台北 |
| 同上 | 4 | 18 步 194 s | 4 次正常 | 0 | 0 | 推荐台北 |

- 「自然语言提示语就不会触发 curl」不成立：天气题搜索成功后模型仍自己 curl 取精确数值；系统提示与工具说明里没有任何一句限制用命令行取网页 / 接口。
- 第 3 次：`brave-api (network), tavily-api (network)` 同时出现，内置 SearXNG 照旧被限流 / 验证码挡住；快速失败按设计生效（17:15:46 `tool_error`）。**根因未定位**：事后 curl 直连 0.3–0.6 s 正常；`requests` 同会话空闲 200 s 复用正常；后端 fd 128 / 256；无钥探针 90 轮 × 4 = 360 次 0 异常；`log show` 该 40 s 无网络 / DNS 变更记录。
- **可观测性缺口**：`search_providers._send` 的 `except Exception` 只留 `kind="network"`，不记异常类名，无法区分超时 / 连接错 / 解析失败。候选加固（未做、未获授权）：网络类失败立即重试一次；记 `type(e).__name__`（不含 URL 与头）。
- 审批卡：非只读判定的 shell 命令（`&& echo` 串联、`| head`、`| python3 -c`）仍会弹卡，「写入自动许可」不覆盖。

### 6.3 用户决定（2026-11-10 傍晚，逐条）
- 「继续用app测试，审批你来点」「保持build85不动」。
- 提议「先用终端方式测试…再用app过一遍」——核实后未采纳：仓库无终端客户端；app 后端访问密钥在钥匙串；审批推送由后端 dispatcher 发（PROTOCOL §2.8），与客户端无关。
- 「换个题测试，纯调查有点简单了，而且压力全丢给web search了，可以设计一个任务，让工具调用尽量全面点，但不过于依赖局部架构。」→ 拟定「本地销售数据（合成 CSV）分析 → 汇总 → 画图 → 一次联网调查 → 写报告并保存 / 打开 →（可选）加日程」的综合题，工具可行性在摸底中，**尚未测**；是否往真实日历写测试日程待用户定。
- 17:55「让我先思考一下，关colab，进度落盘，我压缩一下上下文。」

### 6.4 阶段复审（53e3098a..9860f4e2，stage-reviewer；M1 另经 reviewer 怀疑者复核）
- Critical 0。
- **Major M1（成立）**：`forced_answer` 代码缺省为开，`orch_tools.agent_cfg()` 不写该键 → 下一批膝点测量与 d48b 基线不同口径，混批守卫看不到。已在 `docs/plans/2026-10-31-context-management-plan.md` D49 行与 `benchmarks/data/context-knee-20261109-d48b/ANALYSIS.md` 后续事项补 (c)；代码修补（`agent_cfg()` 显式写键 + `run_knee --forced-answer`，缺省 off，须变异证明）并入 D48c / D49。
- Minor（均未修）：m1 `search_providers.json.corrupt` 与 `.search_providers.json.<hex>.tmp` 未被 `bundle-macos.sh` 排除、未被 `.gitignore` 忽略；m2 `backend/tests/test_web_ssrf.py:40,57-58` 写进了真实算力端 tailnet 地址而仓库为 PUBLIC（换示例地址；历史不改写）；m3 `search_providers` 读文件遇瞬时 OSError 也按损坏缓存，随后一次添加会把好文件改名 `.corrupt`；m4 `run_mutants.py` 判定前不跑基线、不核对红的是否目标用例、逐项日志未入库；m5 三处文档不一致（`backend/README.md:94` 与 `config.yaml:440-441` 仍写 SearXNG 离线则不注册；`AgentClient.swift:867` 注释写 15 s 实为 8 s；`test_forced_answer.py:3` 引用的 `diag/findings.md` 不在库里）。

### 6.5 起机失败与 status 里的「幽灵」记录（11-10 晚查明并修）

用户在 `./colab/deploy.sh status` 里看到一条无主的部署记录，怀疑它是起机失败的原因。只读摸底的结论相反：记录是失败的结果，失败的原因在服务端。

- **现场**：19:05 的 status 显示本机记录 state=`release-unconfirmed`、started 18:59:18、租约 120 分钟、`worker_pid` 指向已退出的进程；服务端 Active assignments 0、sessions 为空、费率 0、余额 456.21。18:59 这次 start 不是本会话发起的（本会话 17:57 停机后没有再起机），18:59:41 还有一次 stop。
- **成因**：CLI 日志里 16:45 与 18:59 两次失败的请求序列相同——分配请求 POST 约 31 s 后回 503，CLI 退避 10 s 重试，再 503，退避 20 s 发第三次；第三次还没返回，`compute_ctl._allocate` 给 `colab new` 的 120 s 超时到点（31+10+31+20+31 ≈ 123 s）。`_allocate` 只捕获 `CalledProcessError`（`colab/compute_ctl.py:867-874`），超时不会标 `allocation_failed`，回收阶段于是判「分配身份未知」并写入 `release-unconfirmed`（`:639-651`）。16:48 的重起第二次 POST 就回了 200，所以成功。
- **为什么卡住下一次起机**：`start` 见到上一条记录不是 stopped 就直接拒绝（`:1525-1526`）；普通 `stop` 清不掉，只有 `stop --confirm-unallocated` 在三次服务端核对都干净后才记为 stopped。这是有意的保守设计：被杀的 CLI 的第三次请求有可能事后分配成功。
- **不是原因的**：没有孤儿进程。`colab` 是单进程脚本，超时后被杀干净；worker 用独立会话脱管是设计如此；旧 worker 改不了新租约（按 session 做 CAS）。`status` 不改状态文件。
- **从什么时候开始的**（CLI 日志全量统计，脚本 `benchmarks/colab_ops/assign_hist.py`，只取时间与状态码）：10-23 到 11-07 共 60 余次分配，全部首发 200、耗时 2–4 s；11-08 12:30 首次变慢（52 s 才回 200），11-08 21:10 起出现 503。11-08 之后共 9 次起机：上午 / 中午 3 次都成（11-08 12:30、11-09 08:31、11-10 09:52），下午 / 晚上 6 次首发全是 503（11-08 21:10、11-09 19:01、11-10 16:45 / 18:59 / 19:20，以及各自的重起），其中 3 次重起在第 2–4 个请求拿到 200。本机没有变化：CLI 0.7.5 装于 10-23，`compute_ctl.py` 最后改动 11-01，此后 11-02 到 11-07 照常首发成功。所以变化在 Colab 服务端；503 的具体原因（多半是 A100 高峰期容量）从客户端无法证实。摸底报告里「11-02 / 11-03 / 11-07 各批首次起机也失败」的推断与日志不符，已作废。
- **已修（11-10 晚，用户指示「失败后自动清理自动重新启动，不断反复重试」「申请到 VM 之前阻塞当前终端，Ctrl+C 终止」）**，改在 `colab/compute_ctl.py`：
  1. `colab new` 带 `COLAB_CLI_ASSIGN_RETRIES=0` 运行，每次只发一次分配请求，本机超时放到 300 s（盖过 CLI 的 GET + POST 各 120 s）。CLI 自己报错退出，走既有的 cli-error → 三次核对 → stopped，不再因为请求途中被杀而落到 `release-unconfirmed`。
  2. `start` 缺省占住终端直到申请到 VM（`start_until_allocated`）：每次尝试是一条新租约加一个照旧脱管的 worker，前台转发 `deploy.log`；申请到即返回 0，后台继续部署。
  3. 只有「服务端明确回 502 / 503 / 504（按 CLI 报错原文末行判定）且三次核对服务端为空」才自动重试，间隔 30 s、不限次数；请求超时、配额 / 权益被拒、`release-unconfirmed`、预检查失败一律停下交给人；worker 失联而租约未停时前台按记录走一次回收。
  4. Ctrl+C / SIGTERM / 关终端：第一发信号即屏蔽后续信号，先打 stop 标记再走既有回收路径，退出码 130。
  5. 别处 `stop` 能终止重试：`request_stop()` 在租约记录上留 `stop_requested`，此后属主不能把租约写回活动态（但它刚从服务端得知的 endpoint / allocation_failed 仍会保留给回收路径），重试循环也不会接续带标记的租约；`stop` 按「打完标记后的记录」决定是否回收。
  6. `--no-retry` 保留旧的「派出即返回」；两个链脚本（`benchmarks/context_knee/run_colab.sh`、`benchmarks/w3_vision_smoke/run_colab.sh`）显式用它，行为除第 1 条外不变。
  - 评审一道门：1 Critical（stop 与重试循环的下一条租约竞态，stop 以为没有活动租约而 VM 照常分配）、2 Major（状态读不出时信任过时的内存租约；CLI 报的请求超时被当成可重试）、6 Minor，均已修或采纳；二道门逐行读，另发现一处（stop 标记挡住了属主把 allocation_failed 写进记录，Ctrl+C 落在被拒的请求途中会先报「回收尚未确认」），已修。
  - 变异证明：23 条 + 2 条全部变红（清单、结果与轮次说明在 `benchmarks/colab_ops/start_retry_mutation/`；M14 以 240 s 硬超时判红）。
  - **实机第一次（20:20，用户自己起的）**：申请一发成功（3 s），但新 VM 的 Jupyter 很慢——`colab exec` 建内核 12 s、连通 30 s（16:49 成功那次各不到 1 s），GPU 检查在原先 60 s 的时限内没回音，worker 按启动失败回收了 VM（存活约 1 分 40 秒）；而前台在「申请到」那一刻已返回 0。请求顺序与成功那次一致，属于 Colab 当晚状态不好的另一面，不是本次改动引入的。
  - **据此再改（用户确认「按这个改」）**：(1) 首次联络新 VM 给 3 分钟（`FIRST_CONTACT`：代码 120 s + 连接余量 60 s），只延长不重发——`gpu_check` 会建私有目录、不可重复执行；(2) 前台等到租约进入 `booting`（GPU 检查通过、部署任务已启动）才返回（`await_boot`）；(3) 申请到之后的失败已在计费，不自动重试，终端报「申请到 VM 后启动失败」；(4) 收到 stop / Ctrl+C 后 worker 不再往 VM 上传凭据或启动部署（`stand_down_if_stopped`，旧缺陷，这一段现在占着终端更容易碰到）；(5) Ctrl+C 回收确认后通知 worker 退出，不再占着租约锁几分钟。增量评审无 Critical / Major，6 条 Minor 采纳 5 条；未采纳的一条写进了 README：CLI 连接内核时自己的单次 10 s 时限与 3 次重试没有动。变异证明另加 7 + 4 条全部变红。
  - **未在真实 Colab 上验证**：CLI 对 503 的报错原文是按 0.7.5 源码与日志里的 `503 Service Unavailable` 推出来的；若实际末行不同，表现是「不属于可自动重试的情形」并停下（失败关闭），需据 `worker.log` 调整判定。
  - 仍未做：status 在 worker 已退出时明说；`deploy.log` 按租约留档；回收阶段 `colab` 调用失败时保留 stderr。
- **未查明**：18:59 的 start / stop 由谁发起；19:00:07–29 每秒一次的用量查询来自哪里。

### 6.6 用户决定（2026-11-10 21:0x，逐条）

- 「现在就可以提交」——§6 的文档与 §6.5 的脚本改动合成一次提交（未说推送，不推）。
- 「新展示题驳回，我再考虑一下新题」——销售 CSV 综合题不采用；展示题待用户另定。
- 「明天我自己起机，你不管」——11-11 的 Colab 起停由用户自己操作，主会话不起机、不停机、不接管。
- 实机补记：用户 20:51 用新脚本自己起机，一发申请到（5 s）、GPU 检查 3 s 通过、20:52:03 终端在租约进入 booting 后返回，随后正常加载模型——新脚本的正常路径第一次在真实 Colab 上走通；503 自动重试路径仍未被真实触发。

### 6.7 新展示方向：旅游题 + 页面剧本 `page`（2026-11-10 22:30–23:30，用户提出，本机 MacAgent build 85）
- **方向（用户）**：旅游主题，联网检索后不只在回答里给结果，而是做成一目了然的 HTML 页面；临时写一份页面设计规范剧本。
- **产物**：`docs/plans/demo-20261111/page.md`（剧本：检索 1–3 次 → 一次 `write_file` 写 `~/AgentPages/<name>.html` → `open_path` 打开 → 2–4 句回复 → 结束；固定 `<style>` 块只许改主题色 `--a`；全页单引号；不引外部资源；各组件条数上限；出处与调查日期进页脚）、`sample-koyo.html`（手工样例，内容为标明的样例数据）、`run1..3-koyo-daytrip.html`（三次彩排的实际产物）。
- **代码事实**：`.html` 属 `open_path` 免审批的查看类（`desktop.py:28-33`）；`write_file` 相对文件名只回 `/api/v1/files/...` 相对链接（`file_edit.py` `_resolve_path`），`open_path` 打不开，故写绝对路径；单次生成缺省上限 4096 token（`engine.py:23`），超了升档 12288 重新生成；工具观察上限 4000 字符（`config.yaml:608`），剧本正文 6.6K 字符经 `skill_invoke` 返回时被截、模型自己再读一次中段（三次都读了，多一步）。
- **安装**：app 设置「台本 → 台本を追加」表单经后台辅助功能填入后「保存」不亮（整框替换的写法不触发 SwiftUI 绑定），改为把文件放进数据根 `skills/page.md` 并重启 app（用户选定）；重启后列表里显示为「手書き」且可用。**展示用的笔记本要另装一次**（同一文件放同一目录后重启 app，或在表单里人工粘贴保存）。
- **彩排**（提示语：「今週末、日帰りで紅葉を見に行きたいです。東京から行けて、今ちょうど見頃の場所を調べて、候補を比べやすいページにまとめてください。」；三次都没点名剧本，模型都自己 `skill_invoke page`）：

| 次 | 发送 → 结束 | 审批卡 | 页面 | 备注 |
|---|---|---|---|---|
| 1 | 23:14:30 → 23:19:07（4 分 37 秒；页面打开在约 3 分 20 秒） | 2（write_file；随后多要一次 `cat` 回读，拒绝） | 5,339 字符，卡片 5（超上限 4），出处 4 | 回复后不收尾：想回读文件，被拒后把整页 HTML 贴进聊天（2,822 tok）|
| 2 | 23:20:25 → 23:23:41（3 分 16 秒，18 步 169 s） | 1 | 4,830 字符，卡片 3，表 5 行，缺值写「確認できず」，出处 3 | 剧本补「`open_path` 返回即结束，不回读、不跑命令、不在聊天里输出 HTML」后干净收尾 |
| 3 | 23:24:20 → 23:27:59（3 分 39 秒） | 1 | 4,473 字符，卡片 3，出处 3 | 干净收尾；回复里多附了一张摘要表（约 510 tok）|

  三次样式块都与骨架逐字一致，零双引号、零外部资源、文件完整闭合。墙钟里含等人点审批卡的 10–35 秒。三次选出的候选地不同（第 1、2 次高尾山 / 昭和記念公園，第 3 次塩原 / 日光 / 高尾山）。
- **未解决**：(a) 页面里的所要时间、费用等数值未逐项对出处核实，第 1 次有明显可疑值（御嶽「新宿から約35分」）；(b) 搜索接口整体失败时的表现本轮没遇到、没测；(c) 剧本正文超 4000 字符被截这一步可以靠压缩正文省掉，未做。
- **起机**：22:47 停旧 VM；22:47:33 首次申请 503 → 自动重试没接上：真实报错被 CLI（rich）按 80 列折行，`TRANSIENT_REFUSAL` 只认单行。改 `transient_refusal()`：取最后一个 `ColabRequestError:` 之后的文本、去掉全部空白再匹配，补 5 个子用例（含真实折行形状）；今晚真实报错原文判为可重试。22:49 重起：第 1、2 次 503 → 自动清理并重试，第 3 次 22:51:57 分到，22:52:05 终端返回，模型随后 READY（**503 自动重试路径首次实机走通**）。**该改动未提交、未过 reviewer 与变异证明**（`colab/compute_ctl.py`、`colab/test_compute_ctl.py`；StartRetryTests 等 21 项 / 37 子项通过）。
- **停机**：23:32:59 用户令停，服务端确认 VM 移除、临时节点注销，计费 0.00/hr，余额 439.38 单位（本台 22:51:57–23:32:59）。
- **截至 23:35 的状态**：未提交的有 `colab/compute_ctl.py`、`colab/test_compute_ctl.py`（折行判定修正，待 reviewer + 变异证明）、本文件 §6.7、`docs/plans/demo-20261111/`（剧本、样例、三次彩排页面）。本机 app 数据根 `skills/page.md` 与仓库草稿一致。待办：展示用笔记本装剧本；用户决定是否采用红叶题为正式展示题；11-11 起机归用户。

### 6.8 build 86：发信 `send_email` + 页面发布 `publish_page`（2026-11-11 上午，分支 `feat/send-email`）

**用户决定（逐条）**：① 展示再进一步——把行程页面展示出来的同时用邮件发给朋友；现有红叶页面题兜底，
在分支上做、赶得上就合并进 main；来不来得及由用户自己判断。② 发信前必须弹窗显示内容摘要和收件人，
确认后才发；只发不读即可。③ 先读 Fastmail 官方说明再定走 SMTP 还是别的。④ 全量模式工具声明预算线可以提，
合并进 main 之前仅限本分支。⑤ 不发文件：正文 = 打招呼 + 一两句说明 + 网页链接；新建一个展示工具，
传入 html、在已部署好的公网目录下建长随机名文件夹放进去、输出链接（发布根目录与域名由用户在 app 设置页填，
不进仓库）。⑥ 提前起机（09:49 申请到，10:08 就绪）。

**为什么走 SMTP**（官方页：fastmail.com/dev、帮助中心 App passwords / Server names and ports）：发信可走 JMAP
或 SMTP；JMAP 发信要 `submission` 权限，而信件对象要先以 `mail` 权限创建（该权限可读可管全部邮件）；SMTP 是
`smtp.fastmail.com:465`、全程 TLS、用户名 = 完整地址、密码 = 应用密码，一次连接发完，换别家邮箱也能用。
应用密码能否限定为「仅 SMTP」官方页未写明。

**设计要点**：
- `send_email(to, subject, body)`：`to` 只能解析到设置页登记的唯一联系人（名字或地址；NFKC、大小写、结尾敬称
  不敏感），未登记地址一律拒；钉死审批（`write=auto` 不放行）且**只认审批卡**（`approval_card_only`）；
  无附件、无重试、证书校验恒开；30 s 内没开始发就放弃。
- `publish_page(path)`：源文件须在 `~/AgentPages` 内（realpath + commonpath，软链拒）、`.html/.htm`、≤ 2 MB；
  在发布根下只新建一个 `secrets.token_urlsafe(24)` 目录和一个文件（0755 / 0644），不覆盖、不删除、不列目录；
  返回 `<base_url>/<token>/<文件名>`。跟随写档策略，不钉死。
- 审批卡预览：工具钩子 `approval_preview` → `pending_tools[i].preview`（`[{key, value}]`），卡上按行显示解析后的
  「名字 <地址>」、主题、正文；批准时重算比对，不一致不执行；卡外调用批准时重过闸门。批准请求新增 `card_ack`，
  含只认卡调用的批次不带它即 409 `card_confirmation_required`、状态不动（PROTOCOL 已写）。
- 设置：数据根 `mail_settings.json` / `page_publish.json`（0600、原子写），SMTP 密码走 `write_secret`
  （名 `MAIL_SMTP_PASSWORD`）；设置页新分区「メール送信」「ページ公開」。
- 页面剧本 `page` 加第 5 步（用户要求发给某人时：发布 → 发信，各一次）；昨晚彩排过的版本存为
  `docs/plans/demo-20261111/page.v1-rehearsed.md`。

**审查**：第一道两轮（首轮 2 Major：推送「允许」按钮可绕过审批卡、挂起后才注册的工具可搭便车执行——均已修，
第二轮判定关闭）；主会话逐行读了两个工具、审批复核、批准接口、设置与密码读写、推送。未做的列在 DEFERRED §11。
变异验证与彩排结果见提交说明与本节后续。

**用户确认（11-11 10:2x）**：发布根的 nginx 没有开目录列表——「链接即凭据」的前提在本次部署上成立
（换部署时仍须自行确认，见 DEFERRED §11）。

**进度（11-11 11:35）**：build 86 已提交到分支 `feat/send-email`（20694f22，未推送、未合并）；发布脚本要求提交在
origin/main 上，用户定「彩排通过后再合并发版」；本机已用 `bundle-macos.sh` 装 build 86（未对外发布）、新版 `page`
剧本已装进 app 数据根。实测：`publish_page` 通过（随机目录 0755、文件逐字节一致、公网 200、根与目录 404）；
`send_email` 先三次被 Fastmail 拒登录——原因是用户改了用户名却没点到邮件分区自己的「保存」（按钮在联系人列表
下方；后端日志 11:15 后无 PUT），保存后 11:32 发送成功。Fastmail 官方：SMTP 登录只认注册时的用户名，别名不行；
防火墙拦 465 时可用 smtps-proxy.fastmail.com 任意端口（设置页暂无端口框）。
**发版前要改**：① 邮件 / 发布分区「保存」按钮位置；② 登录被拒的结果文案写明用户确认改好前不要再调用；
③ 成功文案只说「服务器已接收」。**进行中**：三次完整彩排（红叶题 + 发给联系人「友人A」，用户 11:3x 授权每次发信由主会话批准）。

**三次完整彩排（11-11 11:33–11:47，本机 build 86 本地安装版，Qwen3.8-27B-INT4 / A100）**——提示语：
「今週末、日帰りで紅葉を見に行きたいです。東京から行けて、今ちょうど見頃の場所を調べて、候補を比べやすいページにまとめて、
友人Aさんにメールで送ってください。」三次全部走完：调研 → 写页面 → 打开 → 发布 → 发信；每次三张审批卡（写文件、发布、发信）
由主会话点，发信卡均显示「友人A <地址>」、主题、正文（问候 + 一句说明 + 链接）；后端三次 `[PublishPage] published`、`[SendEmail] sent`，
三个公网链接均 200。

| 次 | 起止 | 墙钟 | app 记的步数 / 用时 | 页面 | 备注 |
|---|---|---|---|---|---|
| 1 | 11:33:26 → 11:38:13 | 4 分 47 秒 | 26 步 / 261 s | 4,636 字符，3 卡，4 出处，样式块与骨架一致 | 先走 `research` 剧本再走 `page` |
| 2 | 11:38:46 → 11:42:52 | 4 分 06 秒 | 16 步 / 182 s | 4,546 字符，3 卡，3 出处，一致 | 过程里露出记忆检索命中的旧会话片段 |
| 3 | 11:43:12 → 11:47:20 | 4 分 08 秒 | 23 步 / 226 s | 4,258 字符，3 卡，3 出处，少了未用到的 `.time` 样式（无害） | — |

墙钟含主会话点卡的延迟（每张约 10–15 秒，三张合计约 35–45 秒）。三次页面都零双引号、零外部资源、文件闭合；存档
`docs/plans/demo-20261111/run{4,5,6}-koyo-mail.html`。
**观察到的问题**：① 三次都在「做页面」那一步显示红色 ✗「iteration budget exhausted (4)」——模型为这道题立了多步计划，
该步要 `skill_invoke` + 两次读回被截断的剧本正文 + `write_file` 共 4 次迭代，正好耗尽步内预算，随后重排计划继续，任务不受影响，
但屏幕上有个 ✗、并多花一次重排；根因仍是剧本正文（约 7.6K 字符）超过 `observation_max_chars` 4000 被截断。
② 页面上的事实（见顷时间、交通、费用）三次候选不同，未逐条对出处核对。③ 页面数字未核对同 §6.7。
