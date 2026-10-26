# MacAgent Apple 原生客户端

SwiftUI 客户端（macOS + iOS 共享同一份源码），直连 MacAgent 后端（FastAPI, 14159）。
本目录源码纳入版本控制；生成的 Xcode 工程、构建产物及签名私钥不入库。macOS 可承载
打包的本地执行层，也可使用开发仓库；iOS 连接已有后端。整体部署见根 [README](../README.md)。

## 构建

工程文件由 XcodeGen 生成（`MacAgentClient.xcodeproj` 不入库，可随时重建）：

```bash
brew install xcodegen   # 已装可跳过
cd apple-app
xcodegen generate
open MacAgentClient.xcodeproj
```

- **macOS**：选 `MacAgent-macOS` scheme 直接 ⌘R（本机免签名配置已就绪）。
  构建产物位置由 Xcode 的 Derived Data 设置或 `-derivedDataPath` 决定。
- **iOS**：选 `MacAgent-iOS` scheme，先在 Signing & Capabilities 里选自己的 Team；
  首次需要在 Xcode ▸ Settings ▸ Components 下载 iOS 平台支持。

## 连接后端

App 内右上角 ⚙ 打开连接设置：

| 设备 | 手填什么 | 不用手填的 |
|---|---|---|
| Mac（app + 后端同机，恒连 `http://127.0.0.1:14159`） | 执行层设置：算力端地址、`INFER_API_KEY`、`RAG_COMPUTE_API_KEY` | `AGENT_API_KEY`：bundle 布局且内建执行层在管时由 app 自动生成并存钥匙串（设置里只显示状态与「重新生成」）；devctl / repo 布局、内建执行层关着、收养了外部后端、或 14159 上的后端自报布局 repo / unknown 时，连接页才显示密钥框，粘贴仓库 `.env.local` 的同值 |
| iPhone/iPad（经 Tailscale） | 无 | 只经 iCloud 钥匙串同步（同一 Apple ID，在 Mac 上点允许）或扫 Mac 上的配对二维码连接；没有地址 / 密钥输入框（2026-10-27 起）。引导页可「稍后配对」，之后在主界面 ⚙ 或直接用系统相机扫码 |

macOS 执行层设置分别保存推理服务与 RAG 服务的连接信息。拆分 RAG 时，算力端对应
`RAG_COMPUTE_API_KEY`，与本机数据面的 `RAG_API_KEY` 分开；旧的远程数据面拓扑仍按
数据面身份配置。远程地址为空时属于未配置，不能据“设置已保存”判断服务可用。

跨设备推荐在 Mac 上用 tailscale serve 反代（后端保持 127.0.0.1 回环绑定不变，
且白得一张合法 HTTPS 证书）：

```bash
tailscale serve --bg 14159
```

Key 保存在系统钥匙串（Keychain），不落盘明文。Info.plist 目前开着
`NSAllowsArbitraryLoads`（允许明文 HTTP，Tailscale 隧道内本就加密）；改用
tailscale serve 的 HTTPS 后可收紧。

## 本地验证

从仓库根运行 `./devctl.sh preflight` 与 `./devctl.sh check`。无签名编译可验证两端源码，
不安装或发布应用（在 `apple-app/` 下执行；首次解析依赖需要网络）：

```bash
xcodegen generate
xcodebuild -project MacAgentClient.xcodeproj -scheme MacAgent-macOS -configuration Debug CODE_SIGNING_ALLOWED=NO build
xcodebuild -project MacAgentClient.xcodeproj -scheme MacAgent-iOS -configuration Debug -destination 'generic/platform=iOS' CODE_SIGNING_ALLOWED=NO build
```

真实模型、服务鉴权、设备交互与推送交付需要独立联调，编译成功不能替代这些验收。

## macOS 下载与自动更新

每个构建一个 GitHub Release（tag `macos-<版本>-<构建号>`），放该构建的 DMG 与升到该版的
delta 补丁；新设备装机下 [Latest Release](https://github.com/enkin-japan/Agent-L0/releases/latest)
的 DMG。应用内更新的 Sparkle `appcast.xml` 单独放在 feed 专用的
[`updates` Release](https://github.com/enkin-japan/Agent-L0/releases/tag/updates)——已安装 app 的
`SUFeedURL` 指着它，这个 tag 不能改名；它只放 appcast，不放安装包（2026-10-26 整理前所有版本
都堆在这一个 Release 里）。

`./release-macos.sh` 负责构建、签名、公证及上传：生成清单后先用 `appcast_release_urls.py` 把下载地址
改写到各构建的 Release；再以草稿建本构建的 Release、上传安装包和补丁、核对远端资产的名/大小/sha256，
核对通过才公开并标 Latest；最后把清单推到两仓的 `updates`。发布前要求工作树干净且 HEAD 已推送到
origin/main（Release 的 tag 指向发布所用的提交）；中途失败重跑时，上次留下的草稿会被删掉重建。
发布说明放 `releases/notes-<构建号>.md`（可选）。迁移前已安装的 build 74 及更早版本仍读取旧仓库
地址，因此发布脚本会把同一份清单同步到旧仓库作为兼容入口；清单中的文件下载地址统一指向
`Agent-L0`。EdDSA 签名针对文件字节，改下载地址不影响验签。

## TestFlight 发布（iOS）

```bash
./release-ios.sh   # xcodegen → Release 归档 → 手动签名 → 直传 App Store Connect
```

- 鉴权：ASC API key，无人值守。**标识符经仓库根 `.env.local`**（`ASC_KEY_ID` /
  `ASC_ISSUER_ID`，模板见 `.env.example`）；**私钥永不入库**，放
  `~/.appstoreconnect/private_keys/AuthKey_${ASC_KEY_ID}.p8`。
  三个发布脚本共用 `asc_env.py` 解析（缺值即明确报错，不会半路 401）。
- 签名：API key（App Manager 角色）无云签名权限，分发证书+私钥自管于
  `macagent-build.keychain`（密码在 `~/.appstoreconnect/macagent-build-keychain-pass`），
  描述文件 "MacAgent iOS AppStore"。证书 2027-07 到期，到期后用 ASC API 重建。
- App 记录：MacAgent（`dev.enkin.macagent`，App ID 6789637764，Team 9A84P4ZS84）。
- 每次发布前 project.yml 的 CURRENT_PROJECT_VERSION 必须自增（TestFlight 要求构建号唯一递增）。
- 上传后苹果处理 5~15 分钟；处理完自动挂内部测试组（asc_wait_build.py 的逻辑），
  iPhone 上 TestFlight 收到更新推送。
- 坑：iPad 支持（TARGETED_DEVICE_FAMILY 含 2）必须声明全部四个 UISupportedInterfaceOrientations，
  否则包校验拒收；`ITSAppUsesNonExemptEncryption=false` 免每构建出口合规确认。

macOS 不走 TestFlight（需上沙盒，收益低）：本机构建 Release 拷 /Applications 即可。

## 架构对应关系

| 本目录 | 对应 web 端 | 说明 |
|---|---|---|
| `Sources/API/Models.swift` | `types/agent-api.ts` | 契约类型（宽容解码） |
| `Sources/API/ProtocolModels.swift` | `types/protocol.ts` | 账本协议 v1 类型（事件信封/turn 状态） |
| `Sources/Store/TurnReducer.swift` | `lib/turn-reducer.ts` | 事件归约器（共享 protocol/fixtures 夹具） |
| `Sources/API/SSEClient.swift` | `api/sse.ts` | SSE 帧解析（心跳注释帧静默跳过） |
| `Sources/API/AgentClient.swift` | `api/agent.ts` | 全部端点封装（202 受理 + 附着流） |
| `Sources/Store/AgentStore.swift` | `store/useChatStore.ts` | 主状态机（归约器投影 + UI 副作用） |
| `Sources/Audio/WavRecorder.swift` | `chat-input.tsx` 录音段 | 16kHz 单声道 WAV，后端 whisper 转写 |

账本协议与 web 端一致（PROTOCOL.md）：`token_delta`/`thinking_delta`/工具事件
只作过程预览，`turn_ended` 的权威 response 才落进消息列表；提交 202 受理即返，
过程与终态经 `GET /events` 附着流观察，断线静默退避重连、带游标重放补断档；
刷新/杀 app/切后台回来经 `/history` + `/turn` + 事件重放完整恢复（含审批卡）。

## 当前功能与行为

- 会话、任务中心、审批、挂起/恢复与断线重放；审批卡支持完整参数并绑定具体轮次/审批批次。
- 停止请求显示提交/受理/拒绝结果，最终由账本终态确认；已完成的外部操作不会自动撤销，执行凭据可查。
- 重试保留原附件和生成设置，成功后替换对话展示，历次尝试仍可查看；“编辑并重发”的既有回退语义不变。
- 定时任务与运行历史经全局 feed + 30 秒兜底快照更新。失败保留最近成功数据并标记过时，未知不当作空闲。
- 会话/文档删除逐项确认，失败项保留选区供重试；删除与同会话在途写请求互斥。明确“目标不存在”可清理本地条目，其他错误不当作成功。
- 会话文件与 Deck 查看、附件上传/录音/粘贴、分支、导出、记忆与知识库管理；远程 Markdown 图片降级为链接，会话资产通过带鉴权的白名单路径加载。
- macOS 知识库页支持主动检查并确认重建，“稍后”不关闭手动入口；未知状态不会强制重建。
- API 工具推荐确认后继续编辑同一声明；保存、上架、重启待生效与远程验证分开。密钥只在设置通道输入，试调可能产生实际副作用。
- macOS 通知消费全局生命周期事件；iOS 已有 APNs 直发通知，内容按设置分级。推送只作提示，返回应用后仍通过后端恢复真实状态，不能保证即时送达。

## 早期功能演进记录

以下按原版本保留历史背景，不是当前完整能力清单；现行行为以本页上节和根协议为准。

第一批：会话列表（pin/删除）、流式对话（思考/工具/任务进度预览）、敏感操作审批卡片、
取消/挂起/恢复、附件上传（文件选择器）、语音输入（WAV）、断线自愈、连接设置。

第二批：定时任务面板（启停/立即运行/中止/删除/运行历史/跳转专属会话；创建编辑仍走
对话里的 schedule_* 工具，与 web 同一设计边界）、会话记忆面板（上下文水位/滚动摘要/
RAG 沉淀，只读）、消息右键「从这一轮分支出新会话」、「删除最后一轮问答」、
图表资产内联渲染（与 web 端同一白名单正则：仅 `/api/v1/files/{sid}/assets/{file}`
相对路径渲染成图；带鉴权加载 + NSCache/URLCache 双层缓存）。

第三批（v0.3.0）：原生 Deck 查看器（DeckViewer 复刻 web slide-renderer 的
1280×720/12×12 网格几何与 role 字号表，同一份 deck.json 双端一致；会话有 deck 时
聊天工具栏出现 ▶ 入口，翻页支持滑动/箭头）、最后一条用户消息右键「编辑并重发」
（后端回退最后一轮 + 回填输入框）、App 在后台时轮次完结/等待审批发本地通知。

第四批（v0.4.0，试用反馈修复）：块级 Markdown 渲染换 swift-markdown-ui
（标题/列表/表格/引用全支持；SPM 依赖，首次构建需联网解析）；用户气泡右键菜单
修复（macOS 上 textSelection 会吃掉自定义菜单，改为菜单内「复制原文」）；
后台通知升级为**全局任务监听**（转后台轮询 /api/v1/tasks，任何客户端——含 web——
发起的轮次完结都会通知；macOS 进程常驻全程有效，iOS 转后台数秒即被系统挂起，
只覆盖挂起前完结的轮次；该版本尚无真正 iOS 推送，后续已加入 APNs（见当前功能））；
iPhone 会话列表选中态卡死修复（返回列表即清 selection）。

第五批（v0.5.0）：iOS 导航换 NavigationStack + navigationDestination(item:)
（修「进入会话即弹回/只能看一次」死锁；pop 时绑定由系统清理）；消息气泡重做
（助手回复带边界气泡；右键/长按菜单挂气泡上修 iOS 预览超界；「复制原文/复制回复」
菜单项 + macOS 悬停复制按钮；文本选中收敛为 iOS-only——macOS 上可选文本会吃掉
自定义右键菜单）。

第六批（v0.6.0）：附件入口升级为菜单——文件选择器 + iOS 相册多选（PhotosPicker）
+ macOS 粘贴剪贴板图片（截图 ⌘⇧4 后直接进对话；Finder 复制的文件保留原名）；
附件 chips 图片显示缩略图；macOS 菜单栏常驻（连接状态/打开窗口/新对话）；
分支操作收敛到助手回复菜单（用户消息菜单去除）。

第七批（v0.7.0）：直接粘贴——macOS 在输入区 ⌘V 即可（Finder 复制的文件→附件、
截图位图→PNG 附件、纯文本正常粘入输入框；菜单里的粘贴项已移除）；iOS 剪贴板有
图片时输入栏旁自动出现系统粘贴按钮（PasteButton 免授权弹窗通道，一点即贴）。

第八批（v0.8.0）：App Intents「问 MacAgent」——Siri / 快捷指令 / 聚焦搜索直接
提问（独立 siri- 会话落列表可回看；系统对后台 Intent 有约半分钟时限，长任务走 App）；
会话导出 Markdown（web export.ts 同口径；「…」菜单 → 导出）；正式 App 图标
（Assets.xcassets，生成脚本见会话记录 gen_icon.swift）。

第九批（v0.9.0，理念对齐 + 体验）：**任务中心**（侧栏顶部 Section：跨会话的
跑/排队/挂起实时快照，挂起可就地恢复，会话行带调度徽标——「重 task 轻会话」）；
**检查器面板**（聊天页 sidebar.trailing 按钮：计划 / 幻灯片缩略图 / 系统提示词 /
工具清单，对齐 web 右侧栏）；**内容搜索**（侧栏搜索框回车触发：客户端拉全会话
history 本地全文扫，按 last_updated 缓存失效；后端搜索端点做好后可换）；
**外观与语言**（浅色/深色/跟随系统；中文/日本語/English 运行时即切，代码表 i18n
以中文为源键缺译回退）；流式渲染 90ms 合帧节流；重新生成回答（助手菜单）；
生成指标小字（tok/tok·s⁻¹）；错误横幅 6s 自动消失。macOS 安装走
`./install-macos.sh`（Release → /Applications）。

第十批（v0.10.0，重整 Phase 2 native——换事件账本脊柱，见根 PROTOCOL.md /
docs/history/REARCHITECTURE.md，后者已归档）：**AgentStore 重写为归约器投影**（`Store/TurnReducer.swift`
实现协议 §5，与 web 端共享 `protocol/fixtures/` 同一批夹具，
`Tests/run_reducer_fixtures.sh` 回放全绿——两端行为一致性的可执行标准）；
发送/审批/恢复改 **202 受理即返**（`/turns` `/turns/approval` `/turns/resume`），
过程与终态从 `GET /events` 附着流回来（断线静默指数退避重连，重放游标补断档）；
**杀 app/切后台回来经 /history + /turn + 事件重放完整恢复**（含审批卡与直播预览，
旧架构"审批中重开=永远转圈"的死局不复存在）；outbox 发送暂存（上传途中被杀，
重开给回收提示）；退役 pollUntilIdle/resync 链、暂存轮热循环与"连接中断"红字
（断开是常态）；顺修存量 bug：工具成功被渲染成 ✗（后端发 "ok" 非 "success"）、
tool_result 详情丢弃、resume 全程无反馈；暂停态（审批/挂起）保留直播预览。
别端（web/定时任务/其他设备）发起的轮次经附着流实时可见。

第十一批（v0.11.0，重整 Phase 3a——粒度直播）：**工具名预告**（`tool_call_pending`
事件:模型还在生成调用参数时,步骤流就出现 "⏳ 工具名…",执行前几十秒可见）；
**思考逐段直播**（thought 频道小段流式,不再整块后置——实测单轮 152 段）；
**排队位次可见**（`queue_moved`:单 worker 忙碌时活动行显示"排队中·位次"）；
归约器 pending 步语义与 web 共享夹具 004 锁定。

第十二批（v0.12.0，Phase 3b + 4a）：任务中心/通知改吃全局 feed（生命周期事件
到达即刷快照,固定轮询降 30s 兜底;后台通知从"消失即完结"猜测改为事件直达,
审批等待也能通知）；旧契约客户端代码删净（chatEvents/AgentEvent 解码层退役,
全走账本协议 202+附着流）;E2E 迁移到账本协议。

第十三批（v0.13.0，Phase 4b 收尾）：会话改名（右键"重命名"→ PATCH /meta,
服务端一等,与 web 双端同源——此前 native 只读服务端派生标题）。**重整工程
（Phase 0~4）到此全部完成**：事件账本脊柱、可寻址轮次、双端瘦渲染器、
协议夹具锁行为、旧契约退役。

当前显示边界：Deck 的 lucide 图标以 SF Symbol 近似映射，未收录时降级为圆点，Web 端也有对应降级。
iOS 后台推送已实现，协议和内容分级见根 [PROTOCOL.md](../PROTOCOL.md) 的推送章节。

已知模型侧问题（非 app）：回复偶发孤立 `*`，web 端同现象，属 gemma 生成格式紊乱，
应在后端提示词/输出清洗层解决。

协议层 E2E：`Tests/run_e2e.sh`（用 app 同一份 API 源码直连真实后端，账本协议：
e2e_client_test 11 项 + e2e_batch2_test 9 项。batch2 的资产用例引用一个现成会话的
图表文件，若该会话被删需换一个路径）。
归约器夹具回放：`Tests/run_reducer_fixtures.sh`（与 web `npm run test` 读同一批
`protocol/fixtures/`，两端行为一致性的可执行标准）。
