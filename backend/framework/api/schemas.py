from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, Literal


class ExecutionReceipt(BaseModel):
    seq: int
    ts: float
    type: Literal["tool_started", "tool_result"]
    name: str = Field(max_length=128)
    call_id: str = Field(max_length=128)
    detail: str = Field(max_length=2000)
    status: Optional[str] = Field(default=None, max_length=32)
    truncated: Optional[bool] = None


class ExecutionReceipts(BaseModel):
    items: List[ExecutionReceipt] = Field(max_length=64)
    truncated: bool


class RetryRequest(BaseModel):
    expected_turn_ref: str = Field(pattern=r"^tr_[0-9a-f]{64}$")
    client_turn_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{8,128}$")

class ChatResponse(BaseModel):
    """
    统一的聊天接口响应体格式
    """
    status: str
    prompt: str
    image: Optional[List[str]] = []
    audio: Optional[List[str]] = []
    # 摄入解析层产物：ingested_text=解析/转写文本（带 provenance 标记，注入上下文的内容）；
    # attachments=原始非原生文件登记（音频/PDF/文档/代码，{path,type,name}，供前端展示/回放）
    ingested_text: Optional[str] = ""
    attachments: Optional[List[Dict[str, Any]]] = []
    enable_thinking: bool
    max_tokens: int
    temperature: float
    min_p: float
    response: List[Dict[str, Any]] # 本轮新增的对话与思考过程

    # status == "pending_approval" 时非空：等待用户授权的敏感工具调用清单
    pending_tools: Optional[List[Dict[str, Any]]] = None
    # 仅投影本轮账本工具开始/返回事实；缺失为未知，不宣称副作用已撤销。
    execution_receipts: Optional[ExecutionReceipts] = None
    turn_ref: Optional[str] = None
    retry_of: Optional[str] = None
    retry_root_index: Optional[int] = None
    retry_parent_index: Optional[int] = None
    retry_input_unavailable: Optional[bool] = None

    # MLX 性能指标
    prompt_tokens: Optional[int] = None
    generation_tokens: Optional[int] = None
    prompt_tps: Optional[float] = None
    generation_tps: Optional[float] = None
    peak_memory: Optional[float] = None

    # 推理仪表（inference-speedup Phase 0）：整轮累计口径
    # token_breakdown：本轮所有生成的 token 去向分解
    #   {thinking, visible, tool_json, marker_overhead}——收益评估的分母
    token_breakdown: Optional[Dict[str, int]] = None
    generations: Optional[int] = None          # 本轮 ReAct 循环内的生成次数
    generation_tokens_turn: Optional[int] = None  # 本轮累计生成 token（区别于末次生成的 generation_tokens）
    parse_error_count: Optional[int] = None    # 本轮工具调用解析失败次数（废轮口径，约束采样要消灭的量）
    # 投机采样接受率（Phase 3 起有值）：drafted=草稿 token 总数，accepted=被验证接受数
    spec_drafted: Optional[int] = None
    spec_accepted: Optional[int] = None

class TaskStatusResponse(BaseModel):
    """
    多步任务进度快照（前端在等待对话响应期间轮询本接口展示计划进度）
    """
    status: bool
    session_id: str
    active: bool                            # 是否存在进行中的多步任务
    generating: bool                        # 该会话当前是否有轮次在推理中（会话锁被持有）
    task: Optional[Dict[str, Any]] = None   # TaskState 快照（goal / plan / 计数器）
    activity: Optional[Dict[str, Any]] = None  # 实时步骤流（phase / current / steps[]）

class TaskListResponse(BaseModel):
    """
    全局任务视图（跨会话）：单 worker 调度器下谁在跑、谁排队、谁挂起。
    供前端「任务切换器」及后续自动调度观测使用。
    """
    status: bool
    running: List[str] = []                  # 占用 worker 的 session_id 们（多 worker 化后为列表，空闲 []）
    queued: int = 0                          # 排队等待的任务数
    suspended: int = 0                       # 已挂起（KV 留内存、可恢复）的任务数
    tasks: List[Dict[str, Any]] = []         # 每项含 session_id / status / kind / 等待或运行时长

class MemoryStatusResponse(BaseModel):
    """
    会话记忆/上下文压缩状态快照（只读，供前端「记忆」面板可视化有界记忆）。
    暴露：当前 model-facing 上下文规模、折叠阈值、已折叠进滚动摘要的水位线、
    摘要体量、以及被折叠原文是否沉淀进 RAG（可经 rag_search 召回）。不触发会话唤醒。
    """
    status: bool
    session_id: str
    loaded: bool                       # 会话是否在内存（热）；冷会话读盘快照
    last_prompt_tokens: int = 0        # 当前 model-facing 上下文规模（prompt token）
    total_turns: int = 0               # 全量落盘历史轮次
    summarized_turns: int = 0          # 已并入滚动摘要的前导历史水位线
    sedimented_turns: int = 0          # 原文已进 RAG 长期记忆的前导历史水位线（零 LLM，独立推进）
    has_summary: bool = False          # 是否已有滚动摘要（发生过折叠）
    summary_chars: int = 0             # 滚动摘要字符数
    high_threshold: int = 0            # 活跃期折叠阈值（prompt token 越过即折叠旧轮）
    low_threshold: int = 0             # 空闲/冻结前折叠阈值
    keep_tail_turns: int = 0           # 折叠时尾部保留的最近轮次数（保持原文）
    sediment_to_rag: bool = False      # 被折叠原文是否沉淀进 RAG（可 rag_search 召回）

class CancelResponse(BaseModel):
    """
    取消当前轮生成/任务的响应体：置位取消标志，编排循环在检查点响应
    """
    status: bool
    session_id: str
    response: str

class SystemPromptResponse(BaseModel):
    """
    会话系统提示词查询响应体（供前端上下文面板展示）
    """
    status: bool
    session_id: str
    system_prompt: str
    source: str  # "session"：该会话在用/已落盘的提示词；"default"：会话尚未创建，返回默认值

class ToolsResponse(BaseModel):
    """
    已注册工具清单响应体（供前端上下文面板展示）
    """
    status: bool
    count: int
    tools: List[Dict[str, Any]]  # 每项含 name / description / parameters / requires_approval

class SkillsResponse(BaseModel):
    """
    剧本清单响应体（斜杠候选提示 / 前端罗列）。含被探活舍弃者
    （available=False + missing 缺失工具清单，前端灰显）。
    """
    status: bool
    skills: List[Dict[str, Any]]  # 每项含 name / description / available / missing

class SkillSaveRequest(BaseModel):
    """剧本保存（UI 设置页面）：name（小写字母数字与连字符）+ description + 正文 body，
    可选 requires（依赖工具名清单，探活用）。写入数据根 skills/{name}.md（用户目录，
    与内置 skills/ 分家；app 模式代码树在签名 bundle 里不可写）。"""
    name: str
    description: str
    body: str
    requires: Optional[List[str]] = None


class SessionFilesResponse(BaseModel):
    """
    会话产物文件清单响应体（检查器「文件」页签的数据源）。
    只列 assets/（Agent 生成/制作/下载的规范出口）；上传原件与任意路径写盘不在此列。
    """
    status: bool
    files: List[Dict[str, Any]]  # 每项含 name / size(字节) / mtime(epoch 秒)，按 mtime 降序

class ApprovalRequest(BaseModel):
    """
    敏感工具审批决议请求体：approve=True 执行挂起的工具调用，False 则拒绝。
    expected_turn_id（可选）：远程决议（推送通知按钮）的防错批锚——决议只对这个
    轮次有效；当前等待审批的轮次不是它时拒绝（409 approval_target_mismatch），
    防止迟到点击批到后续另一个审批请求。
    expected_approval_seq（可选）：卡片对应 awaiting_approval 信封的 seq，
    同轮后续审批批次不匹配时也拒绝；省略时保留旧客户端行为。
    """
    approve: bool
    expected_turn_id: Optional[str] = None
    expected_approval_seq: Optional[int] = Field(default=None, strict=True, gt=0)


class LocationReport(BaseModel):
    """
    客户端定位应答（get_location 工具的回程）：附着客户端收到 location_request
    事件后回报。status=ok 时带坐标；denied/error 时带 error 说明。
    坐标由客户端截到粗粒度（约百米级）后再上报——精确轨迹不进后端。
    """
    request_id: str
    status: str  # ok | denied | error
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    place: Optional[str] = None       # 反地理编码的地名（如 "東京都渋谷区"）
    accuracy_m: Optional[float] = None
    error: Optional[str] = None


class ApiToolSaveRequest(BaseModel):
    """私人 API 工具声明（UI 表单面）：字段与 api_tools/*.yaml 一一对应。
    secret 是随行密钥**实值**——只写数据根 0600 secrets 文件，绝不入声明文件/git。"""
    name: str
    description: str
    endpoint: str
    method: str = "GET"
    params: Optional[Dict[str, Any]] = None
    auth: Optional[Dict[str, Any]] = None
    headers: Optional[Dict[str, Any]] = None
    timeout: Optional[float] = None
    requires_approval: Optional[bool] = None
    enabled: bool = True
    canary: Optional[Dict[str, Any]] = None
    secret: Optional[str] = None

    def to_decl(self) -> Dict[str, Any]:
        """折回 yaml 声明形状（None 字段不落盘；enabled 只在停用时显式写）。"""
        decl: Dict[str, Any] = {"name": self.name, "description": self.description,
                                "endpoint": self.endpoint, "method": self.method}
        for key in ("params", "auth", "headers", "timeout", "requires_approval", "canary"):
            val = getattr(self, key)
            if val is not None:
                decl[key] = val
        if self.enabled is False:
            decl["enabled"] = False
        return decl


class ApiToolTestRequest(ApiToolSaveRequest):
    """试调请求：声明 + 一组实参，真实调用一次并回显响应（不落盘）。"""
    arguments: Optional[Dict[str, Any]] = None


class ApiToolPatchRequest(BaseModel):
    """Only supplied fields change; explicit null restores an optional default."""
    model_config = {"extra": "forbid"}
    description: Optional[str] = None
    endpoint: Optional[str] = None
    method: Optional[str] = None
    params: Optional[Dict[str, Any]] = None
    auth: Optional[Dict[str, Any]] = None
    headers: Optional[Dict[str, Any]] = None
    timeout: Optional[float] = None
    requires_approval: Optional[bool] = None
    enabled: Optional[bool] = None
    canary: Optional[Dict[str, Any]] = None
    secret: Optional[str] = None


class PushRegisterRequest(BaseModel):
    """
    推送设备注册/更新（upsert）：device_id 是 app 生成并持久化的稳定标识；
    token 是 APNs 设备 token（hex）；push_to_start_token 是 Live Activity
    push-to-start token（iOS 17.2+，可无）；prefs 见 PushPrefsRequest。
    """
    device_id: str
    platform: str = "ios"
    token: Optional[str] = None
    push_to_start_token: Optional[str] = None
    prefs: Optional[Dict[str, Any]] = None
    app_build: Optional[str] = None


class PushPrefsRequest(BaseModel):
    """
    通知偏好（部分更新）：三类开关 + 内容分级（full/category/placeholder）。
    """
    results: Optional[bool] = None
    schedule_start: Optional[bool] = None
    approval: Optional[bool] = None
    content_level: Optional[str] = None


class LiveActivityTokenRequest(BaseModel):
    """
    Live Activity 活动 token 上报：app 起活动（本地或 push-to-start）后回传，
    后端据此附着该会话账本做阶段推送。轮次已结束时返回 active=False，
    app 据此本地收尾孤儿活动。
    """
    device_id: str
    session_id: str
    turn_id: str
    token: str

class ScheduleListResponse(BaseModel):
    """
    定时任务清单响应体：每项为 schedule.json 定义 + running（是否正在执行）
    + last_run（最近一次运行记录摘要，无历史则 None）。
    """
    status: bool
    count: int
    schedules: List[Dict[str, Any]]

class ScheduleActionResponse(BaseModel):
    """
    定时任务操作（toggle / run-now / cancel / delete）的统一响应体
    """
    status: bool
    schedule_id: str
    response: str
    schedule: Optional[Dict[str, Any]] = None  # 操作后的最新定义（删除时为 None）

class ScheduleToggleRequest(BaseModel):
    """定时任务启停请求体"""
    enabled: bool

class ScheduleRunsResponse(BaseModel):
    """
    定时任务运行历史响应体（新在前）：每项含 run_id / scheduled_for / started_at /
    finished_at / status / summary / session_id（指向专属会话）
    """
    status: bool
    schedule_id: str
    count: int
    runs: List[Dict[str, Any]]

class DeleteSessionResponse(BaseModel):
    """
    统一的删除会话响应体
    """
    status: bool
    frozen: bool
    response: str
    reason: Optional[str] = None

class DeleteTurnResponse(BaseModel):
    """
    删除最后一轮问答的响应体（内存记忆 / KV Cache / 落盘日志三方同步回退）
    """
    status: bool
    session_id: str
    remaining_turns: Optional[int] = None  # 回退后剩余的历史轮次数
    response: str

class SessionMetaItem(BaseModel):
    """会话索引条目（后端真相，供前端反向映射/对账，与 localStorage 无关）"""
    session_id: str
    title: str                              # 从 history 首条用户消息派生；空壳为 ""
    turn_count: int
    created_at: Optional[float] = None
    last_updated: Optional[float] = None    # history.jsonl mtime；空壳回落 created_at
    pinned: bool = False
    parent_session_id: Optional[str] = None # 分支溯源（悬空仅装饰，不影响功能）
    active: bool = False                    # 调度器视角是否活跃（运行/排队/挂起）

class SessionRetentionPolicy(BaseModel):
    enabled: bool
    retention_days: float
    empty_ttl_hours: float
    scan_interval_seconds: int


class SessionListResponse(BaseModel):
    """会话索引响应体（GET /api/v1/sessions）"""
    status: bool
    count: int
    sessions: List[SessionMetaItem]
    retention_policy: Optional[SessionRetentionPolicy] = None

class PinRequest(BaseModel):
    """置/撤会话 pin 的请求体（pin 的会话不被自动清扫回收）"""
    pinned: bool

class PinResponse(BaseModel):
    """置/撤会话 pin 的响应体"""
    status: bool
    session_id: str
    pinned: bool
    response: str

class RetryGroup(BaseModel):
    root_index: int = Field(ge=0)
    display_index: int = Field(ge=0)
    turn_indices: List[int] = Field(min_length=2)


class HistoryResponse(BaseModel):
    """
    会话历史节点列表响应体（供前端选取分支节点）
    """
    status: bool
    session_id: str
    count: int
    turns: List[Dict[str, Any]]
    # 历史读取前的账本下界；可与已落盘轮重叠，客户端按 turn_index 去重。
    replay_after: Optional[int] = None
    generation: Optional[str] = None
    last_turn_ref: Optional[str] = None
    display_turn_indices: Optional[List[int]] = None
    retry_groups: Optional[List[RetryGroup]] = None

class BranchRequest(BaseModel):
    """
    创建会话分支的请求体
    """
    node_index: int                      # 要分支的历史节点下标（[0, count-1]，支持 -1 表示最后一个）
    new_session_id: Optional[str] = None # 可选：指定分支会话 ID，缺省则自动生成

class BranchResponse(BaseModel):
    """
    创建会话分支的响应体
    """
    status: bool
    session_id: Optional[str] = None        # 新分支会话 ID
    parent_session_id: Optional[str] = None # 源会话 ID
    branch_point: Optional[int] = None      # 实际分支节点下标
    turns: Optional[int] = None             # 分支包含的历史节点数量
    kv_cache_reused: Optional[bool] = None  # 是否成功热复用父会话 KV Cache
    response: str
