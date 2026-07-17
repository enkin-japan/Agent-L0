# codegraph 融合方案评估

> 2026-07-18 过夜评估任务 4/5。对象：`/Volumes/enkin/project/codegraph`
> （colbymchenry/codegraph，MIT，「给编码 Agent 的语义代码智能」——tree-sitter
> 建代码图 + SQLite/FTS5 + MCP 单工具，100% 本地，自带打包运行时无需 Node）。
> 评估问题：如何融合进本 Agent。

---

## 1. 对象是什么

codegraph 把代码仓解析成**符号图**（函数/类/方法为节点，调用/导入/继承/实现为边）
存进项目内 `.codegraph/codegraph.db`（SQLite + FTS5 全文检索），然后以两种方式
提供查询：MCP server（供 Claude Code 等挂载）或 **CLI 命令**。核心卖点：

- **一个强工具**：`codegraph_explore`——一次调用回答「X 怎么工作 / X 怎么到达 Y /
  这块区域有什么」，返回相关符号的**逐字源码（按文件分组）+ 调用路径 + 爆炸半径
  摘要**，能追 grep 追不到的动态分发跳跃（回调、接口→实现）。作者实测结论：
  一个强工具比一排细工具更好引导 agent——**误选更少、省上下文**；
- 索引：`codegraph init` 一次建图，文件监听增量同步（2 秒防抖）；`codegraph sync`
  手动增量；
- 语言：20+，**Python/Swift/TypeScript 全支持**（本仓三大语言全覆盖）；
- 遥测：匿名统计，`CODEGRAPH_TELEMETRY=0` / `DO_NOT_TRACK=1` 永久关闭，关=真关
  （零连接）；
- CLI 与 MCP 工具完全同构：`codegraph explore <query>` 输出与 MCP 工具一字不差，
  另有 query/node/callers/callees/impact/files/status/affected（多数带 `--json`）。

旁证：grok-build 内部有同构自研件（`xai-codebase-graph`，tree-sitter + 定义/引用
/增量索引）——商业编码 Agent 把这个能力当标配，codegraph 等于把它做成了免费外挂。

## 2. 融合价值判断

本 Agent 现有的「看代码」能力 = `find_files`（内容模式）+ 文件读取——能定位
关键词，**追不了跨文件调用流**；模型要自己多轮拼图，恰是 e4b 最弱的环节。
codegraph 的 explore 把多轮拼图压成**一次调用一份紧凑上下文**，图遍历这种
确定性逻辑全部下沉到工具侧——与本项目「架构补模型智力」的路线完全同频，
也与 16GB 的上下文预算约束同频（«surgical context · fewer tool calls» 就是
它的口号）。「一个强工具胜过一排细工具」的实测结论同时旁证了 D4 的方向
（声明面瘦身对小模型尤其要紧）。

使用场景（工作端本地仓库）：用户在对话里问「某项目里 Y 是怎么实现的」「改 Z
会波及哪里」。产出即答案素材，模型只需消化不需拼图。

## 3. 融合方案（建议形态）

**单工具 CLI 包壳**，完全复用 media_download（yt-dlp 包壳）的既有模式：

1. **工具**：`framework/mcp/tools/` 新增 `code_explore.py`，单工具多模式——
   - `explore`（默认）：`codegraph explore <query>`，原文透传（它的输出本就是
     给 agent 设计的格式）；
   - `symbol`：`codegraph node <name>`（单符号源码+调用者）；`impact`：改动波及
     （`--json`）；`status`：图统计；
   - `init`：对指定目录建图（写入该仓 `.codegraph/` + 可能跑几分钟——**过审批
     门**，`timeout_seconds` 提到 900，与 media_download 同律）；
   - 查询类模式前置一次 `codegraph sync`（增量、秒级），免得答案基于陈旧图
     （不引常驻 daemon/文件监听——查询前同步一次的时效性对话场景足够）。
2. **探活**：启动探 `codegraph version`（PATH 上无 = 不注册，S.3 纪律，与
   ffmpeg 探活同模式）。二进制由用户装（installer 一条命令/自更新），**不进
   app bundle**——它是可选外部依赖，与 ffmpeg 同地位。
3. **隐私三件**：工具环境永远注入 `CODEGRAPH_TELEMETRY=0` + `DO_NOT_TRACK=1`
   （不赌用户装机时的选择）；`init` 只对用户显式点名的目录建图（不自动扫盘）；
   索引落在目标仓 `.codegraph/`（它自己的设计），提示用户该目录建议进 .gitignore。
4. **输出闸**：explore 对大仓可能吐几十 KB——套用 grok-build 评估 §3.4 的
   「工具输出进模型前硬上限」（两票合一：那道闸落地时本工具直接受益）。
5. **路径守卫**：projectPath 沿用 find_files 的目录白名单/驻留校验逻辑（不允许
   探到系统目录）。

**排除的路线**：
- 真 MCP 协议对接（挂它的 MCP server）：本项目 `framework/mcp` 是自有插件注册表
  而非 MCP 协议实现，为一个工具引入 MCP 客户端协议栈不成比例；CLI 同构输出使
  协议层毫无增益。若将来要接第二、第三个外部 MCP 工具，再立「MCP 客户端」
  工程票不迟。
- 库嵌入：TypeScript/Node 库，backend 是 Python，进程边界天然隔开，CLI 就是
  正确接口。

## 4. 时机建议

工作量小（一个工具文件 + 金测试，一次实测验收），读侧零风险，可即刻落地。
但按项目纪律（工具面扩张=需求驱动，D2 第三批等用户真实需求清单），**建议归入
D2 第三批候选**，由用户确认「在对话里问代码仓」是不是真实高频需求后再动手。
若用户醒来说「要」，这是五个评估对象里唯一一个当天就能落地的。

一个诚实的保留项：explore 返回的是**源码本身**，消化它仍需代码阅读能力——
e4b 对小问题（单函数怎么实现、谁调用了它）够用，对大问题（整条架构流）恐怕
仍要 31B。工具落地不受此影响（结果可直接展示给用户看），但「模型自主消化后
转述」的质量属模型智力维度，预期要放对。

## 5. 结论

五个对象里融合路径最清晰的一个：**单工具 CLI 包壳 + 探活门控 + 遥测强关 +
init 过审批**，完全套用 media_download 已验证的模式，无新依赖、无新服务、
无架构触碰。价值与「架构补模型智力」「surgical context 省上下文」双同频。
建议作为 D2 第三批候选等用户点头；落地顺带吃到工具输出硬上限那道闸（两票
合一）。

*评估依据：README 全文（MCP Tools/How It Works/CLI Reference/Library Usage/
Supported Languages）、TELEMETRY.md；对照本项目 media_download 包壳模式与
find_files 守卫。未跑其二进制（融合形态评估，不依赖运行时行为）。*
