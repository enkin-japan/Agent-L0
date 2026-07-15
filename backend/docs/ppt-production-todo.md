# PPT 系统：生产机遗留任务（实机迁移后续作指南）

> 本文档假设读者（人或后续 Agent）**没有本次开发的聊天记录**。已建成部分的架构、
> 文件位置、接口契约全部写明；三项遗留任务按依赖顺序排列，每项都标了接入点。
> 设计源头：`/Volumes/enkin/project/agent/PPT.md`（原始蓝图）；本文只记「还没做的」。
> 前提条件：生产机换装 gemma-4-31b（当前开发机 e4b 只够验证工具层）。

## 已建成部分（2026-07，勿重复施工）

单一真相是 **Deck Spec**（`sessions/{id}/deck/deck.json`，deck 归会话），
所有渲染都是它的投影。数据流：

```
模型 deck_set_slide(槽位) → spec.expand_slide 确定性展开成 blocks(12×12 栅格)
  → visual_router.resolve_visuals（chart 即时渲染回填 / image 留占位）
  → deck.json 落盘 → 前端两个投影：
      检视面板 Deck 页签（DeckPreview，实时缩略图+放大浏览）
      /print/deck?session=xxx（打印路由）→ deck_export_pdf 无头 Chrome 出 PDF
```

| 部件 | 位置 |
|---|---|
| Spec 契约/主题 tokens/槽位展开 | `framework/ppt/spec.py` |
| 视觉路由（chart 已通，image 是接入点） | `framework/ppt/visual_router.py` |
| deck 读写/锁语义/增删改 | `framework/ppt/deck_ops.py` |
| PDF 导出（打印前端路由） | `framework/ppt/pdf_export.py`，配置 `config.yaml ppt:` 段 |
| 图表渲染核心（结构化 JSON→PNG，模型不写码） | `framework/agents/chart_tools.py` |
| MCP 工具 | `framework/mcp/tools/deck_tools.py`（deck_init/set_slide/remove/status/export_pdf）、`render_chart.py` |
| 工具的会话归属机制 | `framework/agents/runtime.py` 的 `get_current_session()`（ContextVar，orchestrator `_invoke_tool` 登记） |
| 资产/deck 出货端点 | `framework/api/routes.py` 的 `/api/v1/files/{sid}/assets/{name}` 与 `/api/v1/files/{sid}/deck` |
| 前端渲染器（唯一渲染器） | `frontend/src/components/deck/slide-renderer.tsx` |
| 前端预览/打印视图 | 同目录 `deck-preview.tsx` / `deck-print.tsx`（路由分支在 `main.tsx`） |
| 前端 img 白名单 | `markdown-content.tsx`（只放行 `/api/v1/files/` 同源相对路径，防信标红线不破） |

安全红线（改动时必须保持）：assets 端点双重防穿越（白名单正则 + resolve 包含校验）；
前端 markdown 外域图片一律不加载；`render_chart`/`deck_*` 免审批的依据是「只写本会话
assets/deck 目录、文件名由代码生成」——任何让模型控制落盘路径的改动都要重新过审批设计。

---

## 遗留任务 A：文生图子 agent + 视觉路由 image 分支

**现状**：`image` 块在 spec/渲染器/PDF 里全链路支持，但 `content.asset` 恒为 `null`，
前端渲染为风格化占位块（虚线框 + alt + prompt 文本）。`visual_request` 字段按 PPT.md
契约完整保留在 deck.json 里：

```jsonc
// deck.json 里一个待生成的 image 块（唯一的 pending 判据：kind=image 且 asset=null）
{ "type": "image", "content": { "asset": null, "alt": "hero" },
  "visual_request": { "kind": "image", "prompt": "team celebrating launch" } }
```

**接入点**：`framework/ppt/visual_router.py` 的 `resolve_visuals`，`image` 分支现在只
append 一条占位说明。接入方式（与 chart 分支对齐）：

1. 依 `visual_request.prompt` 生成图片，存 `sessions/{id}/assets/`（文件名用
   `chart_tools.asset_filename(prompt, kind="deck-image")`）；
2. 回填 `block["content"]["asset"] = fname`，`block["visual_request"] = None`；
3. 前端 `slide-renderer.tsx` 已有分支：`image 且 asset 非空 → <img>`，零改动。

**PPT.md 拍板过的两条硬要求**（文生图实现时必须做）：
- **跨页风格一致**：锁 seed + 固定 style prompt（从 `deck["meta"]["tokens"]` 派生），
  不同页的配图要像同一套模板出的；
- **按 prompt hash 缓存**：相同请求不重复生图（成本与一致性双赢）。

**同步/异步取舍**：chart 渲染 ~200ms 走同步；文生图如果要数秒~数十秒，同步会占死
`deck_set_slide` 的工具调用（`agent.tool_timeout_seconds=180` 是硬熔断）。建议：
生成走异步队列（set_slide 先落占位并立即返回，生成完成后改 deck.json 回填），
前端 DeckPreview 本就轮询/事件重取，回填后自动出现。若生产机生图 <10s 也可以先同步糊。

**质量闸门**：图表**绝不**改走文生图（会出假数据、错标签——PPT.md 红线）。

## 遗留任务 B：评审回炉子 agent（渲染后 critique，质量的真正杠杆）

**现状**：完全未做（e4b 上多模态评审没有意义，测了也是测模型下限）。

**为什么值得做**：设计/执行进程都是「盲写」，只有把成品当图看才能发现视觉问题
（标题截断、太挤、对比度差、图文失衡）。PPT.md 认为这是高质量 PPT Agent 的隐藏秘诀。

**施工提示（全部积木都已在场）**：
1. **截图管线**：复用 `pdf_export.py` 的 Playwright 模式，对 `/print/deck?session=xxx`
   逐页 `page.screenshot()`（`.deck-print-page` 元素截图即 1280×720 原图）；
2. **多模态 critique**：gemma-4 原生视觉（`config.yaml inference.modalities` 已含
   image）。子 agent 循环仿 `framework/agents/math_sandbox.py` 的格局：
   `_EphemeralSession` + 手工装配工具 + 主 Registry 不扫描；每次喂一页截图 +
   该页 spec 摘要，让它产出针对 `slide.id` 的结构化修改建议；
3. **回写**：建议翻译成 `deck_ops.set_slide` 调用（或直接 patch blocks——但槽位重展开
   更稳）；**必须跳过 `lock.locked=True` 的页**（deck_ops 已强制拒绝，天然守住）；
4. **收敛**：一轮 critique → 改 → 再渲染即可，别无限回炉（预算参考 math_research
   的 max_steps/total_timeout 配置模式，新开 config 段）。

## 遗留任务 C：共享编辑（阶段一：锁页 UI）

**现状**：锁语义后端已完整（`lock.locked` 字段、set_slide/remove_slide 拒改拒删、
状态在 deck_status 和前端预览页标签里可见），但**没有人工上锁的入口**。

**最小实现**：
1. 后端加 `PUT /api/v1/files/{sid}/deck/slides/{slide_id}/lock`（body `{locked: bool}`），
   走既有 Bearer 鉴权；实现放 `deck_ops.py`（`set_lock(session_dir, slide_id, locked)`，
   顺手把 `dirty_by` 置 `"human"`）；
2. 前端 `deck-preview.tsx` 缩略图角上加锁定开关按钮。

阶段二（块级 dirty_by 细粒度锁、canvas 拖拽编辑回写 spec）按 PPT.md 原文：
**验证阶段一确实不够用了再做**，别提前啃双向同步。

---

## 迁移时的配置检查单

- `config.yaml ppt.frontend_base_url`：生产从 Vite(5173) 换成 Nginx 地址，
  否则 PDF 导出打不开打印路由；
- PDF 导出依赖系统装有 Google Chrome（Playwright `channel="chrome"`，
  与 web_research T2 同一依赖，无需 `playwright install`）；
- matplotlib 已进 `requirements.txt`；CJK 字体链是 macOS 系统字体
  （PingFang SC / Hiragino Sans），非 macOS 部署需换字体栈（`chart_tools._FONT_STACK`
  与 `spec.py THEMES` 的 font 字段）；
- e4b 上观察到、预期 31b 缓解的模型层毛病（工具层无需改）：偶发不先 update_plan
  就逐页糊、槽位里塞多余字段（被 `_clean_*` 静默截断，无害）、长 deck 中途语言漂移。
