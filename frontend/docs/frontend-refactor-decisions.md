# 前端重构 · 架构决策文档(待审批)

> 状态:**草案 / 待拍板**。审批通过后按「分期实施」动工。
> 范围:MacAgent-frontend 重构 + 为支持真流式在后端(MacAgent)加的**最小**改动。
> 原则:**后端为权威**,前端围绕后端既有语义重构,不反向要求后端迁就。

---

## 0. 现状事实(决定改造量的前提)

| 事实 | 出处 | 影响 |
|---|---|---|
| 引擎**已逐 token 生成**,但 `generate_once` 攒成整段才返回 | `engine.py:81` | 真流式缺的是**出口**,不是模型能力 |
| HTTP 层是**同步阻塞**请求/响应 | `routes.py` chat_endpoint | 需新增流式出口(SSE) |
| 前端现靠**轮询 `/task`** 拿进度(activity_snapshot)伪流式 | `routes.py:86` | SSE 后折进事件流,`/task` 降级为断线补偿 |
| **完全无鉴权**,CORS 为 `*` + `allow_credentials`(浏览器会拒的无效组合) | `app.py:198-204` | 从零加鉴权,顺手修 CORS |
| 前端经 **Vite `/api` 代理** → `127.0.0.1:14159`,再经 **Tailscale** 对内网开放 | `api/agent.ts` 注释 | 代理路径下**同源**;网络层已有 Tailscale 鉴权 |
| 一"轮"是**多次生成的事件序列**,有 6 种终态 | `routes.py` | 前端必须是状态机,不是"发→显示" |
| 响应是**增量**(`history[start:]`),非全量历史 | `routes.py:419` | 前端追加,`/history` 仅冷加载/分支 |
| 前端已有 `api/agent.ts` / zustand `useChatStore.ts` / `types/agent-api.ts` | src/ | 在既有骨架上重构 |

---

## 1. 传输层:SSE via fetch-stream(不用 WebSocket,不用原生 EventSource)

**决定**:对话响应改为 **Server-Sent Events**,前端用 **`fetch()` + `ReadableStream`** 消费(不用浏览器原生 `EventSource`)。

**理由**
- 控制动作(取消/审批/挂起)后端**已是独立 POST**,不需要双向通道 → SSE 单向足够,比 WS 简单一个量级,自带重连。
- 原生 `EventSource` **不能设自定义 header**(带不了 Bearer),且**不能 POST**(chat 是 multipart 上传)。两条都被 fetch-stream 解决。
- 这把 Q1(流式)和 Q2(鉴权)绑成同一个决定:**fetch-stream 天然能带 Authorization header**。

**否决**:WebSocket(过度设计,双向没必要);原生 EventSource(带不了 header/不能 POST)。

---

## 2. 鉴权:共享密钥 Bearer,IP 弃用

**决定**:单一共享密钥,前端带 `Authorization: Bearer <token>`,FastAPI 依赖统一校验,失败 401。

**理由 / 细则**
- IP 白名单认证的是"网络位置"不是"客户端":同网段谁都过、换网就废。弃用。
- 比对用 `hmac.compare_digest`(常量时间),禁用 `==`。
- **CORS 顺手修**:走 header Bearer(非 cookie)则**不需要** `allow_credentials`;`allow_origins` 收成真实前端源。
- **密钥不进仓库**:后端从**环境变量**读、`config.yaml` 兜底占位;仓库不放真 key。
- **与 Tailscale 的关系**:Tailscale 已是网络层鉴权,API key 是**纵深防御** + 覆盖"绕过代理直连 14159"的情形。两者不冲突,都留。
- **密钥去哪存(dev)**:推荐 **Vite 代理在转发时注入 header**,密钥只存在于 dev server 端,**浏览器里根本没有 key**(最干净)。生产构建无 Vite 代理时,再退回前端持有 key 或由部署反代注入。→ 见 §9「待拍板」。

**否决**:IP 绑定;明文 query param 传 key(会进日志/URL)。

---

## 3. SSE 事件协议(草案 v0)

一次 `POST /api/v1/chat/{sid}`(multipart)**响应体即 SSE 流**。事件用 `event:` + `data:`(JSON)。**每个事件带 `turn_id` 与 `seq`(单调递增),供去重与断线对齐。**

```
event: turn_start
data: {"turn_id":"t_abc","seq":0}

event: thinking            # 思考块(整块发,不逐字);可选、可多次
data: {"turn_id":"t_abc","seq":1,"text":"...","step":0}

event: token               # 最终答案逐 token(仅"可见答案"频道)
data: {"turn_id":"t_abc","seq":2,"delta":"你好","msg_id":"m_1"}

event: tool_started        # 工具调用开始(结构化,非 token 流)
data: {"turn_id":"t_abc","seq":3,"name":"web_research","args":{...},"call_id":"c_1","step":1}

event: task_progress       # 折进来的原 activity_snapshot:当前动作 + 已完成步骤
data: {"turn_id":"t_abc","seq":4,"plan":[...],"current":"...","done":[...]}

event: tool_result         # 工具结果(可能是 pending_approval 前的产物)
data: {"turn_id":"t_abc","seq":5,"call_id":"c_1","response":"...","status":"ok"}

event: turn_end            # 权威终态 + 完整 response_data(对账用)
data: {"turn_id":"t_abc","seq":9,"status":"success",
       "response":[ ...与现 ChatResponse.response 完全一致的消息数组... ],
       "pending_tools":null,"metrics":{...}}
```

**终态 `status` 六选一**(与后端一致):`success / cancelled / fail / pending_approval / suspended / warning`。
- `pending_approval` → `turn_end.pending_tools` 带待批工具,前端弹审批 UI。
- `suspended` → 前端渲染"恢复"入口。

**核心契约:流为预览,`turn_end` 为真。**
前端渲染分两态:流中显示 `token`/`thinking`/`tool_*` 拼出的**临时气泡**;收到 `turn_end` 后,用其 `response` 数组**替换/对账**该轮内容(权威、已落盘)。若终态是 `cancelled`/`fail`,**撤回或标灰**流中已显示的内容(后端已把该轮整轮回滚出记忆)。

---

## 4. 前端状态机(每会话一个)

```
        ┌───────── idle ──────────┐
        │ send(multipart)         │
        ▼                         │
   streaming ──token/thinking/tool_*──► (自转)
        │                         │
        │ turn_end.status         │
        ├── success ─────────────►│ 落地本轮 → idle
        ├── warning ─────────────►│ 落地(带告警)→ idle
        ├── fail/cancelled ──────►│ 撤回流内容 → idle
        ├── pending_approval ────►│ awaiting_approval
        └── suspended ───────────►│ suspended
                                  
awaiting_approval:
   POST /approval{approve} → (再开一条 SSE)→ 回到 streaming
suspended:
   POST /task/resume       → (再开一条 SSE)→ 回到 streaming
   或用户发新消息 → 后端判定"放弃"(warning),前端须先提示
```

**规则镜像**(后端 `routes.py:334-369` 已强制,前端须在 UI 上体现):
- `awaiting_approval`/`suspended` 状态下**发新消息 = 放弃挂起轮**(后端落 warning)。前端应**禁用输入框**或**二次确认**,别让用户莫名丢任务。
- 控制动作(取消/审批/恢复)是**流开着时的带外 POST**,状态机要允许 streaming 态下并发触发。

---

## 5. 消息模型

一轮 `response` 是**异构消息数组**,前端消息类型围绕 **role + type**,非 user/assistant 二元:

| role | 内容 | 渲染 |
|---|---|---|
| `user` | prompt + 附件(image/audio/ingested_text/attachments) | 用户气泡 |
| `assistant` | thoughts(可折叠)+ text | 助手气泡 + 思考抽屉 |
| `tool` | `tool_responses[]`(name/response/status) | 工具调用卡片(可折叠) |
| `system` | 备注/错误/取消/放弃说明 | 系统提示条 |

- **增量追加**:chat/approval/resume 只返回本轮新增,前端 append;`/history` 仅用于**冷加载会话**与**分支切换**,不每轮重拉。
- `web_research` 等工具结果按普通 `tool` 卡片渲染(其内容已是后端包装的"不可信调查结果",前端不额外解析)。

---

## 6. 断线与补偿

- SSE 断线 → fetch-stream 侧**自动重发**一次 `GET /task` 拿当前 `task_state + activity_snapshot`,按 `seq`/`step` 去重续接;拿到的若已是终态则直接落地。
- 事件**幂等**:`seq` 单调 + `msg_id`/`call_id` 稳定,重连能对齐不重复。
- 页面刷新/冷启动:`GET /history` 重建会话视图,`GET /task` 判断是否有 `pending_approval`/`suspended` 待处理并恢复对应 UI。

---

## 7. 后端改动(最小化,MacAgent 侧)

> 目标:不动 orchestrator/engine 的核心语义,只加"流出口"和"鉴权闸"。

1. **引擎流出口**:`generate_once` 增加可选 `on_delta` 回调(或产出 `asyncio.Queue`),在现有逐 token 循环(`engine.py:97`)里把 `result.text` 推出去;**不改**攒全文+末尾 `parser.parse` 的现有返回(终态仍走原路,保证 `turn_end` 权威)。
2. **SSE 端点**:chat/approval/resume 增加**流式响应变体**(可用 `Accept: text/event-stream` 协商,或新增 `/stream` 路径)。内部仍调 `run_turn`,把编排过程中的 thinking/tool/task_progress/token 作为事件泵到响应流;`run_turn` 返回后发 `turn_end`。**保留非流式端点**作为回退。
3. **鉴权依赖**:FastAPI `Depends` 校验 Bearer;key 从 env 读。挂到全部 `/api/v1/*`。
4. **CORS 收敛**:去 `allow_credentials`,`allow_origins` 收白名单。
5. Orchestrator 需要一条把中间事件"漏"给 SSE 的通道(回调/队列注入)——**唯一稍有侵入**的点,需评估。→ §9 待拍板。

---

## 8. 前端模块划分(在既有 src 上重构)

| 现有 | 重构后职责 |
|---|---|
| `api/agent.ts` | 拆出 `sendChatStream()`(fetch-stream + SSE 解析器);保留 REST 封装(history/tools/cancel/approval/resume/branch) |
| `types/agent-api.ts` | 增补 **SSE 事件类型**(§3)与 6 终态枚举 |
| `store/useChatStore.ts` | 重写为**状态机驱动**:按会话持有状态 + 消息数组;消费 SSE 事件更新临时气泡,`turn_end` 对账落地 |
| (新) `api/sse.ts` | 通用 SSE-over-fetch 解析:分帧、`seq` 去重、断线补偿钩子 |
| (新) `components/*` | 审批卡、恢复卡、工具调用卡、思考抽屉、任务进度条 |

---

## 9. 决策(已拍板 · 2026-07-05)

1. **SSE 端点形态** → ✅ **新增 `/stream` 路径**,旧端点保留作回退。
2. **Orchestrator 事件通道** → ✅ **注入事件回调**(一次到位,工具/思考不再是黑盒)。
3. **dev 密钥存放** → ✅ **Vite 代理注入 header**,浏览器不持有 key。
4. **生产部署形态** → ✅ **Nginx 托管,前后端分离部署**(见 §9.1)。
5. **思考块逐 token?** → ✅ **整块发**,仅正文逐 token。

### 9.1 生产部署(Nginx,前后端分离)

`dist` 由 Nginx 托管;`/api` 反代到后端并**由 Nginx 注入 Authorization**(浏览器不持有 key,与 dev 对称)。SSE 需关代理缓冲。

```nginx
server {
    listen 80;
    server_name agent.example;         # 或 Tailscale MagicDNS 名

    root /var/www/macagent-frontend/dist;
    location / {
        try_files $uri $uri/ /index.html;   # SPA 回退
    }

    location /api/ {
        proxy_pass http://127.0.0.1:14159;
        proxy_set_header Authorization "Bearer ${AGENT_API_KEY}";  # 由 Nginx 注入,浏览器无 key
        proxy_set_header Host $host;

        # SSE 必需:关闭缓冲与超时,保持长连接逐事件下发
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_set_header Connection "";
        proxy_http_version 1.1;
        chunked_transfer_encoding on;
    }
}
```
> Nginx 变量注入 header 需 `set` 或 env module;实际部署可改为写死 key 或用 `map`。后端 `AGENT_API_KEY` 环境变量须与此一致。

---

## 10. 分期实施(审批后)

- **P0 鉴权 + CORS** ✅ **已完成(2026-07-05)**:后端 `framework/api/auth.py` Bearer 依赖(hmac 常量时间)+ env `AGENT_API_KEY` 优先 + `config.yaml server` 段 + CORS 收白名单去 credentials;前端 Vite 代理注入 header(浏览器无 key)+ `.env.example` 补 key。未配置密钥则启动告警并放行(不破坏 dev)。已验证:依赖逻辑 6 例、app 装配 12 路由均挂鉴权、vite.config tsc 通过。
- **P1 SSE 骨架** ✅ **已完成(2026-07-05)**:
  - 后端:`framework/core/stream_filter.py`(VisibleTextStreamer——增量把 token 流过滤成可见正文,抑制思考/工具块,跨块标记安全,`in_thought` 起步态);`engine.generate_once` 加 `on_text_delta` 回调逐段回吐(为 None 时行为不变);`routes.py` 抽出共享闭环 `_chat_turn`(JSON 端点变薄回退),新增 `POST /stream`(SSE:`turn_start`/`token`/`turn_end`,后台任务跑闭环、断连不丢工作)。
  - 前端:`api/sse.ts`(fetch-stream SSE 解析);`api/agent.ts` 加 `sendChatStream` + `buildChatForm`;store `sendMessage` 改流式(临时预览气泡→token 填充→`turn_end` 撤下改用权威 response 对账);`ChatMessage.streaming` 标记。
  - 已验证:VisibleTextStreamer 9 例 + 200 次随机分块不变性;`/stream` TestClient 集成(turn_start→token×3→turn_end、内部键剥离、turn_id 一致、无鉴权 401);前端 tsc + eslint + 生产构建全绿。
  - **真机冒烟已过(2026-07-05)**:真模型下鉴权(无/错 key→401、正确→200);英文 27 事件/中文 45 事件**逐 token 随时间铺开**(跨度~1s);流式拼接与 turn_end 权威正文**逐字一致**(对账);思考模式思考链不泄漏进 token 流、turn_end 单列。
  - **修复(detokenizer 门控)**:SPM 分词器对无空格语言(中文)把文本攒在 `_unflushed`、到 `▁` 词边界/finalize 才 flush,导致 `last_segment` 中途恒空、中文整段结尾一次性吐。引擎改为盯 `detokenizer.text + _unflushed` 做差取增量(`▁`归一空格保前缀单调),中文遂逐字出;`generated_text` 权威累积不变、非 SPM detokenizer 自动退回 last_segment。
  - **顺带修复(P0 回归)**:启用 API key 后 `/api/*` 返回 401 会让 devctl 的 `curl -sf` 探活误判未就绪 → 新增免鉴权 `GET /health`(挂 app 非 router),devctl 探活 URL 改指 `/health`。
  - 待前端浏览器实测:`.env.local` 写 `AGENT_API_KEY` 后 `npm run dev`,发消息看气泡逐字增长。
- **P2 事件全量** ✅ **已完成(2026-07-05)**:
  - 后端:`orchestrator.run_turn` 加 `emit` 回调(决策 #2),在编排关键点发 `thinking`(整块,决策 #5)/`tool_started`/`tool_result`(call_id 配对)/`task_progress`(task+activity 快照);`_execute_physical` 透传 emit;`routes._chat_turn` 加 `emit` 参数,`/stream` 注入统一 emit(所有事件带 turn_id);emit=None 时零开销、非流式路径不变。
  - 前端:`ChatStreamHandlers` 扩展 onThinking/onToolStarted/onToolResult/onTaskProgress,`sendChatStream` switch 分派全部事件;store `sendMessage` **退掉 interval 轮询**,改由 `task_progress` 事件推 taskProgress/liveActivity、`thinking` 挂临时气泡 thoughts;`/task` 仅留轮末 `refreshTaskOnce` 做 catch-up。
  - 已验证(真机):thinking(470字整块)+ task_progress(推"思考中")+ token×144;工具路径 tool_started/tool_result **call_id 配对**、shell 输出正确、工具 JSON 不泄漏进 token 流;非流式 JSON 端点回归正常;前端 tsc+eslint+构建全绿。
  - 待前端浏览器实测:进度/思考/工具卡片实时更新且无轮询请求(Network 面板应无周期性 /task)。
- **P3 状态机 UI** ✅ **已完成(2026-07-05)**:
  - 前端:`ChatResponse.status` 补 `suspended`;store 加 `suspendedSessions` 态 + `applyChatResponse` 处理 suspended(局部消息以 warning 展示、不落 turnCount);新增 `suspendTask`/`resumeTask`/`abandonSuspended` 动作(resume 沿用阻塞端点 + 轮询);发新消息时清可恢复态(镜像后端"放弃"规则,App 层二次确认);**断线重连补偿**——流断而无 turn_end 时查 `/task`,后端已跑完则拉 `/history` 补回权威结果、否则如实提示仍在生成。
  - UI:`ChatArea` 加**恢复卡**(suspended:恢复续跑/放弃)、`ChatInput` 生成中加**挂起(暂停)按钮**(与停止并列);审批卡沿用既有实现。
  - 已验证(真机):空闲 suspend→status False / resume→fail;运行中多步任务 suspend→turn_end **status=suspended**、resume→success 续跑跑完全部步骤;断线中途掉线→后端跑完→`/history` 补回完整正文;前端 tsc+eslint+构建全绿。
  - 说明:approval/resume 续跑仍走**阻塞端点**(非 SSE);其流式化(§4「再开一条 SSE」)作为后续 P4 打磨,不阻塞本期。
  - 待前端浏览器实测:挂起按钮→恢复卡→恢复续跑;断线后自动补回。

---

## 11. 验收标准

- [ ] 无 key / 错 key → 401;Tailscale + 正确 key → 通。
- [ ] 正文逐 token 出现;`turn_end` 后内容与非流式端点**逐字一致**(对账无漂移)。
- [ ] 流中点取消 → 收到 `cancelled` 终态,前端撤回流内容,后端记忆无该轮。
- [ ] 敏感工具 → `pending_approval` 弹审批卡;批准后**再开流**续跑。
- [ ] 挂起 → 恢复卡;挂起态发新消息 → 前端二次确认后落 warning。
- [ ] SSE 断线 → 自动 `/task` 补偿续接,无重复消息。
- [ ] 刷新页面 → `/history` + `/task` 重建视图与待办 UI。
