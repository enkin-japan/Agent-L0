from pydantic import BaseModel
from typing import List, Dict, Any, Optional

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
    running: Optional[str] = None            # 当前占用 worker 的 session_id（空闲则 None）
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

class ApprovalRequest(BaseModel):
    """
    敏感工具审批决议请求体：approve=True 执行挂起的工具调用，False 则拒绝。
    expected_turn_id（可选）：远程决议（推送通知按钮）的防错批锚——决议只对这个
    轮次有效；当前等待审批的轮次不是它时拒绝（409 approval_target_mismatch），
    防止迟到点击批到后续另一个审批请求。
    """
    approve: bool
    expected_turn_id: Optional[str] = None


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

class SessionListResponse(BaseModel):
    """会话索引响应体（GET /api/v1/sessions）"""
    status: bool
    count: int
    sessions: List[SessionMetaItem]

class PinRequest(BaseModel):
    """置/撤会话 pin 的请求体（pin 的会话不被自动清扫回收）"""
    pinned: bool

class PinResponse(BaseModel):
    """置/撤会话 pin 的响应体"""
    status: bool
    session_id: str
    pinned: bool
    response: str

class HistoryResponse(BaseModel):
    """
    会话历史节点列表响应体（供前端选取分支节点）
    """
    status: bool
    session_id: str
    count: int
    turns: List[Dict[str, Any]]
    # 进行中轮次预写标记（{prompt, started_at, gen}）；无则 None。独立于 turns（不参与分支下标）。

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