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
