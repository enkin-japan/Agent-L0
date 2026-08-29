# evaluations/ —— 开源项目评估文档

> 对候选开源项目的架构对比与融合裁决。每项目一文档，结论一行版同步记在
> `docs/history/EXECUTION_LEDGER.md` §3「五开源项目过夜评估」（台账已冻结归档）。更早的三项评估（yt-dlp /
> MediaCrawler / remotion-skills）先于本目录存在，记录在账本 media_download 节。

| 文档 | 对象 | 一句话结论（2026-07-18） |
|---|---|---|
| [grok-build.md](grok-build.md) | xAI 终端编码 Agent（Rust，~60 crate） | 架构信仰同源印证；采纳：压缩前记忆冲洗 / dream 笔记整理 / 审批记忆 / 工具输出硬上限 / MMR |
| [mem0.md](mem0.md) | Agent 记忆层 SDK（YC S24） | 照镜子非引入；采纳：时间锚定纪律 / 标识符子串召回 / 抽取指引并入 memory flush |
| [browser-use.md](browser-use.md) | AI 浏览器操作栈 | 独立第四沙箱形态、绝不融入 web search；挂起等模型智力+需求 |
| [codegraph.md](codegraph.md) | tree-sitter 代码图 MCP/CLI | 单工具 CLI 包壳（media_download 模式），归 D2 第三批候选 |
| [markitdown.md](markitdown.md) | Office→Markdown（微软） | **早已现役**（0.1.6 摄入层+file_extract）；唯一动作=扩展名表名实校准 |

跨文档的合并票（多处评估指向同一改动）：

- **memory flush + 抽取指引**：grok-build §3.1 + mem0 §3.3 → 一张票（压缩摘要
  一次调用双产出：摘要 + 应存笔记的自包含事实句）。
- **工具输出硬上限**：grok-build §3.4，codegraph 落地时直接受益。
- **检索侧小增强**：grok-build §3.7（MMR）+ mem0 §3.2（标识符子串召回），同批
  做进 RAG 数据面 query 管线。
