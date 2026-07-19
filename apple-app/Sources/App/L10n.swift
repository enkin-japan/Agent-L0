import Foundation

/// App 语言（system = 按系统首选语言解析，无匹配回退中文）。
enum AppLanguage: String, CaseIterable {
    case system, zh, ja, en
}

/// 代码表 i18n（与 web 端 lib/i18n.ts 同思路）：键 = 中文原文，缺译回退中文。
/// 语言切换由 AppSettings 写入 current，RootView 以 .id(language) 强制整树重建生效。
enum L10n {
    static var current: AppLanguage = .system

    static var resolved: String {
        switch current {
        case .zh: return "zh"
        case .ja: return "ja"
        case .en: return "en"
        case .system:
            for lang in Locale.preferredLanguages {
                if lang.hasPrefix("zh") { return "zh" }
                if lang.hasPrefix("ja") { return "ja" }
                if lang.hasPrefix("en") { return "en" }
            }
            return "zh"
        }
    }

    static let table: [String: (ja: String, en: String)] = [
        // 通用
        "关闭": ("閉じる", "Close"),
        "保存": ("保存", "Save"),
        "分享": ("共有", "Share"),
        "复制": ("コピー", "Copy"),
        "重新生成": ("再生成", "Regenerate"),
        "重启中…": ("再起動中…", "Restarting…"),
        "已更新": ("更新しました", "Updated"),
        "完成": ("完了", "Done"),
        "选择多个": ("複数選択", "Select Multiple"),
        "删除所选": ("選択項目を削除", "Delete Selected"),
        "确认删除所选会话？": ("選択したチャットを削除しますか？", "Delete selected chats?"),
        "（不可恢复）": ("（元に戻せません）", "(cannot be undone)"),
        "重新生成会立即重启执行层；其他设备须重新填入新 Key":
            ("再生成すると実行レイヤーが直ちに再起動します。他のデバイスでは新しいキーの再入力が必要です",
             "Regenerating restarts the execution layer immediately; other devices must re-enter the new key"),
        "取消": ("キャンセル", "Cancel"),
        "删除": ("削除", "Delete"),
        "恢复": ("再開", "Resume"),
        "好": ("OK", "OK"),
        "轮": ("ターン", "turns"),
        "工具": ("ツール", "Tool"),
        // 侧栏 / 会话
        "新对话": ("新規チャット", "New Chat"),
        "详细信息": ("詳細", "Details"),
        "复制全文": ("全文をコピー", "Copy full text"),
        "密钥即改即存入钥匙串；对正在运行的执行层，点下方重启后生效。":
            ("キーは即座にキーチェーンへ保存されます。実行中の実行レイヤーには下の再起動で反映されます。",
             "Keys are saved to Keychain immediately; restart below to apply to a running execution layer."),
        "重启执行层": ("実行レイヤーを再起動", "Restart execution layer"),
        "已存长度": ("保存済みの長さ", "Stored length"),
        "选择或新建一个会话": ("チャットを選択または新規作成", "Select or create a chat"),
        "左上角 ✎ 开始新对话": ("左上の ✎ から新規チャット", "Tap ✎ to start a new chat"),
        "搜索会话内容…": ("チャット内容を検索…", "Search chat contents…"),
        "正在全文搜索…": ("全文検索中…", "Searching…"),
        "没有命中的会话": ("該当するチャットはありません", "No matching chats"),
        "取消置顶保留": ("ピン留め解除", "Unpin"),
        "置顶保留": ("ピン留めして保持", "Pin & Keep"),
        "删除会话": ("チャットを削除", "Delete Chat"),
        "已连接": ("接続済み", "Connected"),
        "未连接": ("未接続", "Disconnected"),
        "连接中…": ("接続中…", "Connecting…"),
        "会话": ("会話", "Chats"),
        "连接设置": ("接続設定", "Connection Settings"),
        // 任务中心
        "任务": ("タスク", "Tasks"),
        "对话": ("チャット", "Chat"),
        "续跑": ("再開実行", "Resuming"),
        "记忆压缩": ("メモリ圧縮", "Memory compaction"),
        "定时任务": ("スケジュール", "Schedules"),
        "审批续跑": ("承認後続行", "Post-approval"),
        "已运行": ("実行中", "running for"),
        "排队中 · 已等待": ("待機中 · 経過", "Queued · waiting"),
        "已挂起（进度保留）": ("一時停止中（進捗は保持）", "Suspended (progress kept)"),
        // 聊天
        "挂起": ("一時停止", "Suspend"),
        "恢复续跑": ("再開する", "Resume Task"),
        "任务已挂起（推理资源已让出，进度保留在后端）":
            ("タスクは一時停止中（推論リソースを解放、進捗はバックエンドに保持）",
             "Task suspended (compute released; progress kept on backend)"),
        "生成中…": ("生成中…", "Generating…"),
        "思考中…": ("思考中…", "Thinking…"),
        "步骤": ("ステップ", "Step"),
        "任务推进中": ("タスク進行中", "Task in progress"),
        "思考过程": ("思考プロセス", "Reasoning"),
        "会话记忆…": ("チャットメモリ…", "Chat Memory…"),
        // 知识库管理面板（P1；入口在设置——全局数据实体归设置，不占会话菜单）
        "知识库": ("ナレッジベース", "Knowledge Base"),
        "数据": ("データ", "Data"),
        "加载中…": ("読み込み中…", "Loading…"),
        "确认删除所选文档？": ("選択したドキュメントを削除しますか？", "Delete selected documents?"),
        "用户未授权定位": ("ユーザーが位置情報を許可していません", "User has not granted location permission"),
        // 私人 API 工具管理面板
        "API 工具": ("APIツール", "API Tools"),
        "添加 API 工具": ("APIツールを追加", "Add API Tool"),
        "编辑 API 工具": ("APIツールを編集", "Edit API Tool"),
        "还没有 API 工具声明": ("APIツールの宣言はまだありません", "No API tool declarations yet"),
        "确认删除该声明？": ("この宣言を削除しますか？", "Delete this declaration?"),
        "已注册的实例要重启后端才会真正下架。": ("登録済みのインスタンスはバックエンド再起動後に外れます。", "Registered instances are removed only after a backend restart."),
        "已删除声明；运行中的实例在重启后端后下架": ("宣言を削除しました。稼働中のインスタンスは再起動後に外れます", "Declaration deleted; the running instance unloads after restart"),
        "已删除": ("削除しました", "Deleted"),
        "仓库 api_tools/ 里的手写声明只读；在这里添加的声明保存于数据目录。修改或删除已注册的工具需重启后端生效。": ("リポジトリ api_tools/ の手書き宣言は読み取り専用。ここで追加した宣言はデータディレクトリに保存。登録済みツールの変更・削除は再起動後に反映。", "Hand-written declarations in repo api_tools/ are read-only; ones added here are stored in the data directory. Edits/deletions of registered tools apply after a backend restart."),
        "手写": ("手書き", "custom"),
        "编辑": ("編集", "Edit"),
        "坏声明": ("不正な宣言", "Invalid"),
        "已上架": ("登録済み", "Active"),
        "已停用": ("停止中", "Disabled"),
        "未上架": ("未登録", "Not registered"),
        "必填": ("必須", "Required"),
        "工具名": ("ツール名", "Tool name"),
        "说明（模型全凭这句话决定何时调用）": ("説明（モデルはこの文だけで呼ぶかどうか判断）", "Description (the model decides when to call based on this alone)"),
        "地址（可含 {参数} 路径模板）": ("URL（{param} パステンプレート可）", "Endpoint (may contain {param} path templates)"),
        "方法": ("メソッド", "Method"),
        "工具名用小写蛇形（如 weather_home）。说明要写清触发场景与返回内容，例如\"查询我家里 NAS 的状态，用户问 NAS 时调用\"。": ("ツール名は小文字スネークケース（例 weather_home）。説明には呼ぶ場面と戻り内容を明記。", "Use lowercase snake_case for the name (e.g. weather_home). The description should state when to call it and what it returns."),
        "参数（可选）": ("パラメータ（任意）", "Parameters (optional)"),
        "参数名": ("パラメータ名", "Param name"),
        "参数说明": ("パラメータの説明", "Param description"),
        "必需": ("必須", "Req"),
        "添加参数": ("パラメータを追加", "Add parameter"),
        "扁平参数覆盖大多数私人 API；嵌套结构请手写 yaml。路径模板里的 {参数} 也要在此声明。": ("フラットなパラメータで大半のAPIをカバー。ネスト構造は yaml を手書き。パステンプレートの {param} もここで宣言。", "Flat parameters cover most private APIs; write yaml by hand for nested schemas. Declare path-template {params} here too."),
        "鉴权（可选）": ("認証（任意）", "Auth (optional)"),
        "密钥环境变量名（如 MY_API_KEY）": ("キーの環境変数名（例 MY_API_KEY）", "Key env var name (e.g. MY_API_KEY)"),
        "请求头（默认 Authorization）": ("ヘッダー（既定 Authorization）", "Header (default Authorization)"),
        "格式（默认 Bearer {key}）": ("形式（既定 Bearer {key}）", "Format (default Bearer {key})"),
        "密钥值": ("キーの値", "Key value"),
        "密钥值（已配置，留空保持不变）": ("キーの値（設定済み。空欄で変更なし）", "Key value (configured; leave blank to keep)"),
        "密钥值保存在后端数据目录的受限文件里（0600），不进声明文件、不进 git、不回传。": ("キーはバックエンドのデータディレクトリの権限付きファイル（0600）に保存。宣言にも git にも入らず、返送もされません。", "The key is stored in a 0600 file under the backend data directory — never in the declaration, git, or responses."),
        "高级（可选）": ("詳細（任意）", "Advanced (optional)"),
        "超时秒（默认 30）": ("タイムアウト秒（既定 30）", "Timeout seconds (default 30)"),
        "审批": ("承認", "Approval"),
        "跟随方法默认": ("メソッドに従う（既定）", "Method default"),
        "强制审批": ("常に承認", "Always require"),
        "免审批": ("承認不要", "Never require"),
        "启用": ("有効", "Enabled"),
        "默认：GET/HEAD 免审批，变更类方法（POST 等）每次调用弹审批。停用的声明保留但不注册。": ("既定：GET/HEAD は承認不要、変更系メソッドは毎回承認。停止中の宣言は保存のみで未登録。", "Default: GET/HEAD skip approval; mutating methods prompt each call. Disabled declarations are kept but not registered."),
        "试调": ("テスト実行", "Test call"),
        "试调值：": ("テスト値：", "Test value: "),
        "试调一次": ("一回テスト実行", "Test once"),
        "试调中…": ("テスト実行中…", "Testing…"),
        "按当前表单真实调用一次并回显响应（不保存）。保存前先试调可即时发现声明错误。": ("現在のフォーム内容で実際に一回呼び出して応答を表示（保存なし）。保存前のテストで宣言ミスを即発見。", "Really calls once with the current form and shows the response (nothing saved). Testing before saving catches mistakes immediately."),
        "已保存并上架": ("保存して登録しました", "Saved and registered"),
        "已保存但探活未过（检查密钥），补齐后重扫可上架": ("保存しましたが疎通チェック未通過（キーを確認）。補完後の再スキャンで登録可", "Saved but the availability probe failed (check the key); rescan after fixing"),
        "已保存；该工具已在运行中，重启后端后生效": ("保存しました。稼働中のツールはバックエンド再起動後に反映", "Saved; the tool is currently running — takes effect after backend restart"),
        "已保存（停用状态，不会注册）": ("保存しました（停止中のため未登録）", "Saved (disabled, not registered)"),
        "已保存": ("保存しました", "Saved"),
        // 剧本管理面板（用户以 /名字 点名的流程提示词）
        "剧本": ("台本", "Playbooks"),
        "添加剧本": ("台本を追加", "Add Playbook"),
        "编辑剧本": ("台本を編集", "Edit Playbook"),
        "还没有剧本": ("台本はまだありません", "No playbooks yet"),
        "确认删除该剧本？": ("この台本を削除しますか？", "Delete this playbook?"),
        "删除后立即下架，该 /名字 不再可点名。": ("削除すると直ちに無効化され、/名前 で呼び出せなくなります。", "Removed immediately; the /name can no longer be invoked."),
        "已删除并下架": ("削除して無効化しました", "Deleted and unloaded"),
        "已删除（重启后端后下架）": ("削除しました（バックエンド再起動後に外れます）", "Deleted (unloads after backend restart)"),
        "剧本 = 用户以 /名字 点名的流程提示词。仓库内置的只读；在这里添加的保存于数据目录。": ("台本 = ユーザーが /名前 で呼び出す手順プロンプト。リポジトリ内蔵は読み取り専用、ここで追加したものはデータディレクトリに保存。", "A playbook is a flow prompt you invoke with /name. Repo built-ins are read-only; ones added here are stored in the data directory."),
        "内置": ("内蔵", "Built-in"),
        "缺依赖": ("依存不足", "Missing deps"),
        "名字（小写字母数字与连字符）": ("名前（小文字・数字・ハイフン）", "Name (lowercase, digits, hyphens)"),
        "名字只能小写字母或数字开头，后接小写字母、数字或连字符。": ("名前は小文字か数字で始まり、以降は小文字・数字・ハイフンのみ。", "Name must start with a lowercase letter or digit, then lowercase letters, digits, or hyphens."),
        "说明（一句话点明这套流程做什么）": ("説明（この手順が何をするか一言で）", "Description (one line on what this flow does)"),
        "正文 = 点名 /名字 时注入的流程提示词。": ("本文 = /名前 で呼び出した際に注入される手順プロンプト。", "The body is the flow prompt injected when you invoke /name."),
        "可选": ("任意", "Optional"),
        "依赖工具（逗号或空格分隔）": ("依存ツール（カンマまたはスペース区切り）", "Required tools (comma or space separated)"),
        "列出该流程需要的工具名；缺任一个则剧本探活不过，候选里灰显为「缺依赖」。": ("この手順が必要とするツール名を列挙。1つでも欠けると台本は疎通チェックに通らず、候補で「依存不足」とグレー表示。", "List the tool names this flow needs; if any is missing, the playbook fails its probe and appears greyed as “Missing deps.”"),
        "已保存并立即生效": ("保存して即時反映しました", "Saved and now live"),
        "已保存（缺依赖，补齐后可用）": ("保存しました（依存不足、補完後に利用可）", "Saved (missing deps; usable once resolved)"),
        "已保存（重启后端后生效）": ("保存しました（バックエンド再起動後に反映）", "Saved (takes effect after backend restart)"),
        "知识库为空": ("ナレッジベースは空です", "Knowledge base is empty"),
        "RAG 服务离线": ("RAG サービスがオフライン", "RAG service offline"),
        "我的知识": ("マイナレッジ", "My Knowledge"),
        "Agent 记忆": ("エージェントの記憶", "Agent Memory"),
        "这是 Agent 的长期记忆（对话沉淀与笔记镜像）。删除后 Agent 将失去相应回忆；笔记镜像删除后仍会随笔记改写再次生成。":
            ("エージェントの長期記憶（会話の沈殿とノートのミラー）です。削除すると該当する記憶が失われます。ノートのミラーは削除してもノート更新時に再生成されます。",
             "This is the agent's long-term memory (folded conversations and note mirrors). Deleting it makes the agent forget; note mirrors regenerate when notes are rewritten."),
        "篇": ("件", "docs"),
        "此分类暂无文档": ("このカテゴリにドキュメントはありません", "No documents in this category"),
        "删除分类": ("カテゴリを削除", "Delete Category"),
        "删除文档": ("ドキュメントを削除", "Delete Document"),
        "确认删除文档？": ("ドキュメントを削除しますか？", "Delete this document?"),
        "切片将从检索中清除，原文移入回收目录（可手工找回）。":
            ("チャンクは検索から除去され、原文はゴミ箱ディレクトリへ移動されます（手動で復元可能）。",
             "Chunks are removed from search; the raw file is moved to a trash folder (recoverable by hand)."),
        "确认删除分类及其全部文档？": ("カテゴリと全ドキュメントを削除しますか？", "Delete this category and all its documents?"),
        "这是 Agent 的长期记忆，删除后 Agent 将失去相应回忆。原文移入回收目录（可手工找回）。":
            ("これはエージェントの長期記憶です。削除すると該当する記憶が失われます。原文はゴミ箱ディレクトリへ移動されます（手動で復元可能）。",
             "This is the agent's long-term memory; deleting it makes the agent forget. Raw files go to a trash folder (recoverable by hand)."),
        "该分类的全部文档将从检索中清除，原文移入回收目录（可手工找回）。":
            ("このカテゴリの全ドキュメントが検索から除去され、原文はゴミ箱ディレクトリへ移動されます（手動で復元可能）。",
             "All documents in this category are removed from search; raw files go to a trash folder (recoverable by hand)."),
        "已标记过时": ("古い情報の可能性", "Marked stale"),
        "切片": ("チャンク", "chunks"),
        "未索引": ("未インデックス", "Not indexed"),
        "导出会话 (Markdown)…": ("チャットを書き出す (Markdown)…", "Export Chat (Markdown)…"),
        "删除最后一轮问答": ("最後のやり取りを削除", "Delete Last Turn"),
        "查看演示文稿": ("スライドを表示", "View Slides"),
        "检查器（计划 / 幻灯片 / 文件 / 工具集）":
            ("インスペクタ（計画 / スライド / ファイル / ツール）",
             "Inspector (Plan / Slides / Files / Tools)"),
        // 文件页签
        "文件": ("ファイル", "Files"),
        "打开": ("開く", "Open"),
        "当前会话还没有产物文件。图表、导出的 PDF、下载的音视频会出现在这里。":
            ("このセッションにはまだ成果ファイルがありません。チャート、書き出したPDF、ダウンロードした音声・動画がここに表示されます。",
             "No output files in this session yet. Charts, exported PDFs, and downloaded media will appear here."),
        "保存失败：": ("保存に失敗：", "Save failed: "),
        "下载失败：": ("ダウンロードに失敗：", "Download failed: "),
        // 消息菜单
        "复制原文": ("原文をコピー", "Copy Text"),
        "已复制": ("コピーしました", "Copied"),
        "复制回复": ("返信をコピー", "Copy Reply"),
        "复制回复（markdown 原文）": ("返信をコピー（markdown 原文）", "Copy reply (raw markdown)"),
        "从这一轮分支出新会话": ("このターンから分岐", "Branch From This Turn"),
        "编辑并重发（回退这一轮）": ("編集して再送信（このターンを取り消し）", "Edit & Resend (rolls back)"),
        "重新生成回答": ("回答を再生成", "Regenerate Reply"),
        "调用": ("呼び出し", "Call"),
        "返回": ("の結果", "result"),
        "工具调用解析失败": ("ツール呼び出しの解析に失敗", "Tool call parse error"),
        "媒体": ("メディア", "media"),
        // 输入区
        "给 Agent 发消息…": ("Agent にメッセージ…", "Message the Agent…"),
        "选择文件…": ("ファイルを選択…", "Choose Files…"),
        "照片图库…": ("フォトライブラリ…", "Photo Library…"),
        "添加附件（图片 / PDF / 文档 / 音频）": ("添付を追加（画像 / PDF / 文書 / 音声）", "Add attachment (image / PDF / doc / audio)"),
        "语音输入（松开即作为音频附件发送给后端转写）": ("音声入力（停止で音声添付として送信・転写）", "Voice input (transcribed on backend)"),
        "深度思考模式": ("深い思考モード", "Deep thinking mode"),
        "取消本轮（后端整轮回滚）": ("このターンを中止（バックエンドでロールバック）", "Cancel turn (rolls back)"),
        "麦克风权限被拒绝": ("マイクへのアクセスが拒否されました", "Microphone Access Denied"),
        "请在系统设置中允许本 App 使用麦克风。": ("システム設定でマイクの使用を許可してください。", "Allow microphone access in System Settings."),
        "（见附件）": ("（添付参照）", "(see attachments)"),
        "缺依赖：": ("依存不足：", "Missing: "),
        // 审批
        "以下敏感操作等待你的授权": ("以下の機密操作が承認待ちです", "Sensitive operations awaiting your approval"),
        "拒绝": ("拒否", "Deny"),
        "批准执行": ("承認して実行", "Approve"),
        // 设置
        "后端地址": ("バックエンドURL", "Backend URL"),
        "API Key": ("API Key", "API Key"),
        "AGENT_API_KEY（存入钥匙串）": ("AGENT_API_KEY（キーチェーンに保存）", "AGENT_API_KEY (stored in Keychain)"),
        "与仓库根 .env 里的 AGENT_API_KEY 相同；后端未启用鉴权时留空。":
            ("リポジトリ直下 .env の AGENT_API_KEY と同じ。認証無効時は空欄。",
             "Same as AGENT_API_KEY in repo-root .env; leave empty if auth is off."),
        "本机运行填回环地址；跨设备走 Tailscale：推荐在 Mac 上 `tailscale serve` 反代 14159，这里填 https://<mac>.<tailnet>.ts.net":
            ("同一Mac上なら 127.0.0.1。他デバイスからは Tailscale 経由：Mac 側で `tailscale serve` を推奨、ここに https://<mac>.<tailnet>.ts.net",
             "Use loopback when local; across devices use Tailscale: run `tailscale serve` on the Mac and enter https://<mac>.<tailnet>.ts.net"),
        "测试连接": ("接続テスト", "Test Connection"),
        "测试中…": ("テスト中…", "Testing…"),
        "❌ 地址无效": ("❌ URLが無効です", "❌ Invalid URL"),
        "❌ 后端返回异常": ("❌ バックエンド応答が異常", "❌ Backend returned an error"),
        "✅ 连接与鉴权正常": ("✅ 接続・認証OK", "✅ Connection & auth OK"),
        "⚠️ 服务可达，但 API Key 不正确": ("⚠️ 接続可、ただし API Key が不正", "⚠️ Reachable, but API Key is wrong"),
        "❌ 无法连接：": ("❌ 接続できません：", "❌ Cannot connect: "),
        "❌ 域名无法解析，请确认本设备 Tailscale 已连接且启用了 Tailscale DNS":
            ("❌ ホスト名を解決できません。Tailscale の接続と Tailscale DNS を確認してください",
             "❌ Cannot resolve host — check Tailscale connection and Tailscale DNS"),
        "❌ 域名可解析但端口不通：确认对端 tailscale serve / 后端进程在运行。":
            ("❌ 解決はできるが接続不可：tailscale serve とバックエンドの稼働を確認",
             "❌ Resolves but can't connect — check tailscale serve / backend process"),
        "App 版本": ("Appバージョン", "App Version"),
        "外观": ("外観", "Appearance"),
        "语言": ("言語", "Language"),
        "跟随系统": ("システムに従う", "System"),
        "浅色": ("ライト", "Light"),
        "深色": ("ダーク", "Dark"),
        "外观与语言": ("外観と言語", "Appearance & Language"),
        // 定时任务面板
        "暂无定时任务": ("スケジュールはまだありません", "No schedules yet"),
        "在对话里让 Agent 创建，例如「每天早上 9 点帮我总结新闻」":
            ("チャットで Agent に作成を依頼（例：毎朝9時にニュース要約）",
             "Ask the Agent in chat, e.g. “summarize news at 9am daily”"),
        "下次": ("次回", "Next"),
        "上次": ("前回", "Last"),
        "中止本次": ("今回の実行を中止", "Cancel Run"),
        "立即运行": ("今すぐ実行", "Run Now"),
        "运行历史": ("実行履歴", "History"),
        "收起历史": ("履歴を閉じる", "Hide History"),
        "查看": ("表示", "View"),
        "操作失败：": ("操作に失敗：", "Operation failed: "),
        // 记忆面板
        "会话记忆": ("チャットメモリ", "Chat Memory"),
        "加载失败": ("読み込みに失敗", "Failed to load"),
        "上下文水位": ("コンテキスト使用量", "Context Usage"),
        "当前上下文": ("現在のコンテキスト", "Current context"),
        "冷会话（KV 已释放）": ("コールド（KV解放済み）", "Cold (KV released)"),
        "折叠阈值": ("圧縮しきい値", "Compaction threshold"),
        "滚动摘要": ("ローリング要約", "Rolling Summary"),
        "全量历史": ("全履歴", "Total history"),
        "已折叠": ("要約済み", "Summarized"),
        "摘要体量": ("要約サイズ", "Summary size"),
        "摘要": ("要約", "Summary"),
        "尚未产生": ("未生成", "Not yet"),
        "保留尾部": ("末尾保持", "Tail kept"),
        "轮不折叠": ("ターンは圧縮しない", "turns uncompacted"),
        "长期沉淀": ("長期蓄積", "Long-term"),
        "折叠原文入 RAG": ("圧縮原文を RAG へ", "Sediment to RAG"),
        "开启（可被检索召回）": ("有効（検索で想起可能）", "On (retrievable)"),
        "字符": ("文字", "chars"),
        // 检查器
        "计划": ("計画", "Plan"),
        "幻灯片": ("スライド", "Slides"),
        "工具集": ("ツール", "Tools"),
        "当前会话没有进行中的多步任务计划": ("進行中のマルチステップ計画はありません", "No active multi-step plan"),
        "当前会话没有演示文稿。让 Agent「做一份 PPT」即可生成。":
            ("スライドはありません。Agent に「PPTを作って」と頼むと生成されます。",
             "No deck yet. Ask the Agent to “make a deck”."),
        "放映": ("再生", "Present"),
        "需审批": ("要承認", "Approval"),
        "（读取失败）": ("（読み込み失敗）", "(failed to load)"),
        // 菜单栏
        "正在生成…": ("生成中…", "Generating…"),
        "打开 MacAgent": ("MacAgent を開く", "Open MacAgent"),
        "退出": ("終了", "Quit"),
        // Store 消息
        "会话列表加载失败：": ("チャット一覧の読み込みに失敗：", "Failed to load chats: "),
        "历史加载失败：": ("履歴の読み込みに失敗：", "Failed to load history: "),
        "连接中断，任务在后端继续，正在重新同步…": ("接続が切断。タスクはバックエンドで継続中、再同期しています…", "Disconnected; task continues on backend, resyncing…"),
        "分支失败": ("分岐に失敗", "Branch failed"),
        "分支失败：": ("分岐に失敗：", "Branch failed: "),
        "回退失败": ("ロールバックに失敗", "Rollback failed"),
        "回退失败：": ("ロールバックに失敗：", "Rollback failed: "),
        "有敏感操作等待你的授权": ("機密操作が承認待ちです", "A sensitive operation awaits your approval"),
        "本轮执行失败，回来看看详情": ("実行に失敗しました。詳細を確認してください", "Turn failed — check details"),
        "任务已完成": ("タスク完了", "Task completed"),
        // 通知设置 / 推送
        "通知": ("通知", "Notifications"),
        "启用通知": ("通知を有効にする", "Enable Notifications"),
        "任务结果": ("タスク結果", "Task results"),
        "鉴权要求（建议保持开启）": ("承認リクエスト（オン推奨）", "Approval requests (keep on)"),
        "定时任务开始": ("定期タスク開始", "Scheduled task started"),
        "通知内容": ("通知の内容", "Notification content"),
        "完整内容": ("フル内容", "Full content"),
        "仅类别（推荐）": ("種別のみ（推奨）", "Category only (recommended)"),
        "仅提示有新事件": ("新着のみ通知", "New-event ping only"),
        "系统已拒绝本 App 的通知权限——请到系统设置里为 MacAgent 打开。": (
            "システム設定で通知が拒否されています。MacAgent の通知を有効にしてください。",
            "Notifications are denied in System Settings — enable them for MacAgent."),
        "iPhone 通知经 Apple 推送服务（APNs）投递；「仅类别」不把任务内容发出 Mac。灵动岛与锁屏实时活动随任务自动出现。": (
            "iPhone への通知は Apple のプッシュサービス（APNs）経由です。「種別のみ」ならタスク内容は Mac の外に出ません。Dynamic Island とロック画面のライブアクティビティは自動で表示されます。",
            "iPhone notifications go through Apple Push (APNs); “Category only” keeps task content on your Mac. Dynamic Island / Lock Screen Live Activities appear automatically."),
        "Mac 通知完全本地投递，不经任何外部服务。": (
            "Mac の通知は完全ローカル配信で、外部サービスを経由しません。",
            "Mac notifications are delivered entirely locally — no external service."),
        "允许执行": ("実行を許可", "Allow"),
        "等待授权：": ("承認待ち：", "Awaiting approval: "),
        "定时任务遇到需授权操作，已自动拒绝": (
            "定期タスクが承認必須の操作に遭遇し、自動拒否されました",
            "Scheduled run hit an approval wall — auto-denied"),
        "定时任务开始运行": ("定期タスクが開始しました", "Scheduled task started"),
        "任务完成 ✓": ("タスク完了 ✓", "Task done ✓"),
        "任务失败 ✗": ("タスク失敗 ✗", "Task failed ✗"),
        "任务完成（有警告）": ("タスク完了（警告あり）", "Task done (with warnings)"),
        "任务结束": ("タスク終了", "Task ended"),
        "有新事件，点开查看。": ("新着イベントがあります。開いて確認。", "New event — open to view."),
        "这条审批已过期或已被处理，未生效。": (
            "この承認は期限切れか処理済みのため、反映されませんでした。",
            "This approval expired or was already handled — no effect."),
        "无法连接后端，审批未送达——任务仍在等待你的决定。": (
            "バックエンドに接続できず、承認は届いていません——タスクは引き続き待機中です。",
            "Couldn’t reach the backend; approval not delivered — the task is still waiting."),
        "启动中": ("起動中", "Starting"),
        "已完成 ✓": ("完了 ✓", "Done ✓"),
        "执行失败 ✗": ("失敗 ✗", "Failed ✗"),
        "已结束": ("終了", "Ended"),
        "轻点卡片进 App 允许": ("許可はカードをタップして App で", "Tap card to allow in app"),
        // —— 账本协议(Phase 2/3)——
        "准备调用": ("呼び出し準備中", "Preparing to call"),
        "排队中 · 下一个执行": ("待機中 · 次に実行", "Queued · next up"),
        "排队中 · 前面还有": ("待機中 · 前に", "Queued · ahead:"),
        "超时": ("タイムアウト", "timeout"),
        "上次可能有一条消息未送达：「": ("前回、未送信の可能性があるメッセージ：「", "A message may not have been delivered last time: \""),
        "」——请确认后按需重发。": ("」——確認のうえ必要なら再送してください。", "\" — please check and resend if needed."),
        "该会话已有正在进行的轮次，请等它完结后再发送。": ("この会話には進行中のターンがあります。完了を待ってから送信してください。", "This conversation already has a turn in progress. Wait for it to finish."),
        "发送失败：": ("送信失敗：", "Send failed: "),
        "重命名": ("名前を変更", "Rename"),
        "重命名会话": ("会話の名前を変更", "Rename conversation"),
        "标题": ("タイトル", "Title"),
        "审批提交失败：": ("承認の送信失敗：", "Approval submit failed: "),
        "恢复提交失败：": ("再開の送信失敗：", "Resume submit failed: "),
    ]
}

/// 界面文案查表：以中文为源键，缺译回退中文。
func L(_ zh: String) -> String {
    switch L10n.resolved {
    case "ja": return L10n.table[zh]?.ja ?? zh
    case "en": return L10n.table[zh]?.en ?? zh
    default: return zh
    }
}
