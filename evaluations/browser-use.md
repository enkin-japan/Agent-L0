# browser-use 融合形态评估

> 2026-07-18 过夜评估任务 3/5。对象：`/Volumes/enkin/project/browser-use`
> （browser-use/browser-use，「让 AI 像人一样用浏览器」——开页/点击/输入/填表，
> MIT 系许可，Python）。评估问题：对本 Agent 有无补充？融入 web search 好还是
> 独立工具好？

---

## 1. 对象是什么

browser-use 是一个**自带完整 Agent 循环的浏览器操作栈**，三层结构：

- **browser 层**：CDP 驱动真浏览器（`cdp-use`），会话/多标签/上传下载/截图；
- **dom 层**：把页面序列化成 LLM 可读文本——**可交互元素带索引编号**（模型说
  「点 23 号」而不是写选择器），另有 markdown 正文抽取、增强快照；
- **agent 层**：自己的编排循环（MessageManager/SystemPrompt/judge 自评/GIF 回放/
  云事件遥测），**默认 vision 优先**（每步截图喂给模型），自带 LLM 抽象层
  （openai/anthropic/google/groq/ollama/…全家桶）。

动作集：search/navigate/click（按索引或坐标）/input/upload/切换关闭标签/extract/
scroll/send_keys/find_text/screenshot/save_as_pdf/…。另有 MCP server 模式与
skill 模式（供 Claude Code 等外部 agent 挂载）。

**依赖面（要害）**：pyproject 全部 `==` 精确钉死（openai/anthropic/groq/google
四家 SDK + posthog 遥测 + bubus 事件总线 + cdp-use + darwin 下 pyobjc），塞进
本项目 backend venv 必然版本打架；posthog/云事件是商业遥测，须显式关闭。

## 2. 与本项目 web_research 的关系：互补，不是替代

本项目 `web_research`（研究沙箱）的自我定义（web_tools.py 头注）：**无登录公开
网页的只读调查**——SearXNG 搜索 + T0 静态→T1 curl_cffi 指纹→T2 Playwright
无头渲染的抓取升级梯 + 反投毒清洗 + SSRF 硬拒绝 + 信任边界包装。

browser-use 的本质是**有副作用的交互操作**：点击、提交表单、登录态、上传。
两者是「读」与「写」的物种差异：

| | web_research（现有） | browser-use |
|---|---|---|
| 动作性质 | 只读（渲染也是只读） | 写/操作（点击/提交/登录） |
| 审批 | 无需（只读调查沙箱不变量） | 必须（每个动作都可能不可逆） |
| 反投毒 | 核心价值（正文=不可信资料） | 无此概念（页面内容直接进模型） |
| 抓正文能力 | T2 渲染已覆盖 JS 站 | 有（markdown_extractor）但非其卖点 |

**注意**：抓正文这件事 browser-use 并不比本项目强——T2 渲染用的同样是
Playwright，反投毒/SSRF/信任边界反而是本项目独有。引 browser-use「增强 web
search」得不到新能力，只得到依赖地狱。

## 3. 拍板问题：融入 web search 还是独立工具？

**结论：独立工具（形态上是第四个沙箱），且挂起等模型智力到位。绝不融入 web search。**

### 3.1 为什么不融入 web search

与 MediaCrawler 同一判据（账本已有先例，2026-07-18 用户拍板放弃那次）：把
「有副作用、可能有登录态、每步需审批」的动作混进**无审批只读调查沙箱**，
边界直接脏掉——研究沙箱今天敢不弹审批自主抓几十个页面，正是因为它被构造性
地限制为只读；一旦沙箱里能点按钮，这个自由度就必须收回，整个 web_research
的可用性反而倒退。**读写分离是本项目沙箱体系的地基，不能为便利拆掉。**

### 3.2 为什么现在挂起

- **模型智力**：browser-use 默认 vision 优先（每步截图进模型），驱动真实网页
  UI 的规划/恢复能力远超 e4b-4bit；16GB 上每步一张截图的多模态推理在延迟上
  也不现实。属 D1 用户纪律「模型智力引发的事故等生产设备再优化」的典型场景。
- **真实需求未现**：用户尚无「让 Agent 替我填表/订票/操作网站」的需求清单；
  D2 第三批同样在等需求定型。无需求先行付出依赖面与安全面成本，违背本项目
  「需求驱动工具面」的节奏。

### 3.3 届时落地的建议形态（预研结论，落地等拍板）

1. **第四沙箱「浏览器操作」**：与 math/excel/research 并列的物理隔离沙箱，
   **独立 venv**（隔离其 `==` 钉死依赖），posthog/云事件全关。
2. **只取 browser/dom/tools 三层，不搬它的 agent 循环**：它的循环与本项目
   R1「子 Agent 与主轮同一执行器」纪律冲突（两套循环=两套方言/审批/纠错），
   且其 LLM 抽象层是本项目方言层的重复建模。正确姿势 = research_sandbox 模式：
   本项目的受限循环 + browser-use 的 BrowserSession/DOM 序列化/动作注册表做
   执行底座。
3. **审批语义**：导航/读取类动作沙箱内自主；click/input/upload/表单提交一律
   过审批门（与 shell 工具同律）。登录态用系统 Chrome profile（CDP 挂真浏览器，
   与抓取梯 T2 复用系统 Chrome 的思路同源）。
4. **备选轻路线**：若届时需求只是「偶尔点一下」，可不引库——Playwright 本项目
   已有，DOM 索引化序列化（其真正的核心技术）约几百行可自研，动作集只做
   click/input/navigate 三件。需求定型前两条路线都不动。

## 4. 顺手可借鉴（不引库、与拍板无关）

- **敏感数据占位符**（`_detect_sensitive_key_name`）：用户密钥以占位符名进
  prompt（模型看 `{{GITHUB_TOKEN}}` 不看值），运行时替换真值。将来任何「工具
  需要凭据」的场景（表单填充、API 调用类工具）都该用这个模式，与本项目
  「浏览器永不持钥」同一信仰的工具侧版本。
- **judge 自评模块**：任务收尾用一次「裁判调用」判断是否真完成。本项目收尾
  审计（无计划超阈值轮次的最终答复审计）已是同思路，互为印证，无需动。
- **DOM 索引化交互元素**的序列化思路：若未来做 macOS GUI 自动化工具（AX 树
  → 索引化元素表），这是被验证过的表示法，D2/D3 届时参考。

## 5. 结论

browser-use 对本项目**有真实补充**（交互式网页操作是 web_research 构造性做不了
的），但**不是现在**：模型智力与真实需求两个前置都未到位。方向上一锤定音的是
形态：**独立第四沙箱、绝不融入 web search**（读写分离地基不拆）；落地时只取其
browser/dom 执行底座、不搬其 agent 循环（R1 纪律）。当下动作 = 零：不引依赖、
不加工具，仅把「敏感数据占位符」模式记入将来凭据类工具的设计要求。

*评估依据：browser_use/tools/service.py（动作集全扫）、agent/service.py（循环
与 vision 默认）、dom/ 模块结构、llm/ 提供方清单、pyproject.toml 依赖面、
README；对照本项目 web_tools.py 头注与抓取梯实现。未跑其代码。*
