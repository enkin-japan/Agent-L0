"""批处理泳道（§7-0① 并发编排层接线，2026-08-22；首过评审后重写）。

双泳道设计（为什么长这样）：
- 现有单流路（前缀复用/T8 门/上位解/工具 guard/drafter/KV 持久化）**一字不动**——
  它承载的正确性修复全部依赖逐 token 单流语义，重建在上游 runtime 上等于全部重验。
- 本泳道只收**显式 opt-in**（features.batch_ok=true）的纯文本请求：冷预填、无会话
  缓存、无 guard、无思考频道。凡带单流路专属特性的请求一律路由回单流路
  （route_reason 判定，fail-closed：判不清 = 不进泳道）。
- 引擎 = 上游 mlx-vlm BatchGenerator（generate/ar.py，纯引擎零 HTTP 耦合），与
  已装载模型**共享权重**（model.language_model 同一对象，零双载）。
  ⚠️ prefill_batch_size **必须为 1**：上游准入门是
  `completion_batch_size - active >= prefill_batch_size`（ar.py `_next`），两值相等
  时行只能在批完全排空后加入 = 静默退化成串行——正是并发格要测的东西的反面
  （首过评审 C3）。设 1 后随到随进（有空位即可），代价只是新行逐个预填。
- 线程纪律：上游 insert/next/remove 彼此**不**线程安全（上游全在单 GPU 线程调用），
  故本泳道同样只允许驱动线程触碰生成器；submit/cancel 只投递意图（队列+集合），
  驱动线程独享执行权。行级 detokenizer 从工厂启动时制备的模板克隆
  （make_streaming_detokenizer 语义：copy + reset），不在生成中途读单流路正在
  改写的共享 detokenizer。
- 交互优先（Q14 宪章草案的机械保证）：驱动线程在**每个引擎步边界**（decode 步
  或预填块，预填块粒度 prefill_step_size=2048 tok）查 interactive_busy()，单流路
  在生成期间本泳道整体让路——「后台流让位排队」的准入纪律就落在这一行。
- 流隔离：不传 stream，让上游 BatchGenerator 用它缺省的 generation_stream——
  那是 **ThreadLocalStream**（每线程惰性各得一条私流），驱动线程自动与单流路
  执行器线程分流；权重只读共享，缓存各自独立。⚠️ 不得换成 mx.new_stream：
  那种流绑定创建线程的注册表，跨线程用即
  "There is no Stream(gpu, N) in current thread"（首轮真机冒烟实炸）。
- 行输出队列**无界**（评审 C1）：单行产出天然被 max_tokens 封顶（≤几千个小 dict），
  而有界队列 + 停读客户端 = 驱动线程卡死在 put 上、整条泳道连同取消/让路/收摊
  全部冻结。内存上限换不冻结，这买卖必须做。
- 采样语义：泳道级 sampler（上游批引擎的结构约束——sampler 建在批上不在行上），
  按待处理请求的 (temperature, min_p) 分代构建；同参共批，不同参等泳道排空后
  重建生成器（廉价）。temp=0 时 min_p 不参与身份（贪心路它是死参数，评审 m18）。
  repetition_penalty 为逐行 logits 处理器；注意批路的惩罚上下文从生成 token 起算
  （上游 token_context 语义），与单流路「prompt 播种」不同——裸贪心测量口径
  （rep=None）不受影响，生产背景任务用到时此差异已知情。
- 已知耦合（评审 M15，今日字面回环故为潜伏）：BatchGenerator 绑定共享
  tokenizer.stopping_criteria，而单流路每次生成会 reset 它——当前 gemma-4 的
  config eos 与 tokenizer eos 集合相同故无实害；换模型/改 eos 集合前须重审。

CPU-only 可测性：本模块不在 import 时触碰 mlx / config；上游生成器经
build_generator 工厂惰性构建，金测试注入假生成器即可驱动全部状态机。
"""
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# 驱动线程空转节拍：有活但被交互让路/无活等唤醒时的轮询间隔（秒）。
# 一个 decode 步几十 ms，10ms 粒度让路延迟远小于一步。
_POLL_S = 0.01

# 泳道拒收理由（route_reason 返回值，空串=可进泳道）。
# 全部列成常量：server 日志与金测试都锚这些字面量，不许自由发挥。
REASON_NOT_OPTED = "not_opted_in"          # 未带 batch_ok（缺省一切走单流路）
REASON_NOT_ARMED = "lane_not_armed"        # max_concurrency<=1 或泳道已 dead
REASON_MEDIA = "has_media"                 # 图/音频（视觉编码是单流路的事）
REASON_TOOL_GUARD = "has_tool_specs"       # 工具约束 guard 只在单流路生效
REASON_THINKING = "has_thinking"           # 思考预算/思考频道走单流路自家 criteria
REASON_SESSION_CACHE = "has_cache_id"      # 会话缓存/前缀复用是单流路语义
REASON_DELTA_FEED = "has_prompt_delta"     # token 级增量喂入依赖会话缓存
REASON_SPECULATIVE = "speculative_active"  # 投机（drafter/prompt-lookup）不进泳道：
                                           # 批路静默退非投机 = 口径分母污染（13:57 型）


def route_reason(body: Dict[str, Any], *, armed: bool, has_speculative: bool) -> str:
    """判定一个 /v1/generate 请求能否进批处理泳道。

    返回空串=可进；否则返回拒收理由常量。fail-closed：任何单流路专属特性在场
    即拒——批路静默忽略 guard/缓存/思考等于把约束悄悄丢掉（§S.3 同族）。
    """
    features = body.get("features") or {}
    if not features.get("batch_ok"):
        return REASON_NOT_OPTED
    if not armed:
        return REASON_NOT_ARMED
    if has_speculative:
        return REASON_SPECULATIVE
    if body.get("images") or body.get("audios"):
        return REASON_MEDIA
    if features.get("tool_specs"):
        return REASON_TOOL_GUARD
    if features.get("thinking_budget") is not None or features.get("enable_thinking"):
        return REASON_THINKING
    if body.get("cache_id"):
        return REASON_SESSION_CACHE
    if features.get("prompt_delta_offset") is not None:
        return REASON_DELTA_FEED
    return ""


def sampling_key(sampling: Dict[str, Any]) -> Tuple[float, float]:
    """泳道级采样身份：同 (temperature, min_p) 才共享一代生成器。

    缺省值与单流路 stream_generate 的缺省逐字对齐（temp 0.2 / min_p 0.05）——
    评测面即部署面，两条泳道的缺省口径不许分家。temp=0 走贪心路，min_p 是
    死参数，归一为 0 免得 (0,0.05)/(0,0) 无谓分代（评审 m18）。
    """
    temp = float(sampling.get("temperature", 0.2))
    min_p = float(sampling.get("min_p", 0.05)) if temp != 0 else 0.0
    return (temp, min_p)


@dataclass
class _Pending:
    req_id: str
    prompt: str
    sampling: Dict[str, Any]
    want_delta: bool
    out: "queue.Queue[Tuple[str, Dict[str, Any]]]"
    skey: Tuple[float, float] = (0.0, 0.0)


@dataclass
class _Row:
    req_id: str
    out: "queue.Queue[Tuple[str, Dict[str, Any]]]"
    streamer: Any
    want_delta: bool = True
    n_tokens: int = 0
    prompt_tokens: int = 0
    prompt_tps: float = 0.0
    t_first: Optional[float] = None


class BatchLane:
    """上游 BatchGenerator 直驱的并发生成泳道（驱动线程独享生成器）。

    build_generator(skey) -> 生成器上下文对象，须提供：
      tokenize(prompt) -> List[int]；embed(input_ids) -> prompt_kwargs dict；
      row_streamer() -> 行级流式 detokenizer（.advance(tok, finish)/.finalize()）；
      row_processors(rep, rep_ctx)；peak_memory_gb()；decoding_count()；
      insert/next/remove/has_work —— 上游 BatchGenerator 协议（remove 返回 False
      = 行正被多行预填批处理暂时摘不下来，调用方须重试）；close() —— 释放。
    生产实现见 build_upstream_context；金测试注入假对象驱动状态机。
    """

    def __init__(self, *, build_generator: Callable[[Tuple[float, float]], Any],
                 max_num_seqs: int,
                 interactive_busy: Callable[[], bool],
                 poll_s: float = _POLL_S):
        self._build = build_generator
        self.max_num_seqs = max(1, int(max_num_seqs))
        # 在途上限（评审 M13 背压）：超过即 submit 拒收 → server 退单流路，
        # 由 gen_sem 的排队语义自然限流，而不是无界攒线程池等待者
        self.max_inflight = max(8, 4 * self.max_num_seqs)
        self._interactive_busy = interactive_busy
        self._poll_s = poll_s
        self._pending: "queue.Queue[_Pending]" = queue.Queue()
        self._holdback: List[_Pending] = []     # 采样参数不合当前代的等待区
        self._cancelled: set = set()            # req_id：意图集合，驱动线程消化
        self._rows: Dict[int, _Row] = {}        # uid -> 行（仅驱动线程读写）
        self._req2uid: Dict[str, int] = {}      # 仅驱动线程读写
        self._ctx = None                        # 当前代生成器上下文（驱动线程独享）
        self._ctx_skey: Optional[Tuple[float, float]] = None
        self._wake = threading.Event()
        self._stop = False
        self.dead = False                       # 引擎异常/收摊后永久 fail-closed
        self._active_count = 0                  # 对外仪表：泳道在册行数（含排队）
        self._decoding_count = 0                # 对外仪表：真在 decode 批里的行数
        self._paused = False                    # 对外仪表：正被交互让路
        self.steps_total = 0                    # 对外仪表：引擎步数
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()

    # ---- 对外（事件循环线程）----

    def submit(self, prompt: str, sampling: Dict[str, Any], *,
               want_delta: bool = True
               ) -> Tuple[str, "queue.Queue[Tuple[str, Dict[str, Any]]]"]:
        """投递一个请求。返回 (req_id, 输出队列)。

        输出队列元素：("chunk", payload) / ("done", payload) / ("error", payload)，
        chunk payload 键位与单流路 SSE chunk 对齐（终态 payload 各带各的附注键，
        客户端按未知键忽略处理）。队列无界（原因见模块头 C1 条）。
        """
        if self.dead or self._stop:
            raise RuntimeError("批处理泳道不可用（dead/stopped）")
        # 背压（近似读驱动线程侧结构，量级判断足够）：满了退单流路
        if (self._pending.qsize() + len(self._holdback)
                + len(self._rows)) >= self.max_inflight:
            raise RuntimeError("批处理泳道在途已满（背压退单流路）")
        req_id = uuid.uuid4().hex
        out: "queue.Queue[Tuple[str, Dict[str, Any]]]" = queue.Queue()
        p = _Pending(req_id=req_id, prompt=prompt, sampling=dict(sampling or {}),
                     want_delta=bool(want_delta), out=out)
        p.skey = sampling_key(p.sampling)
        self._pending.put(p)
        # 与 _die/shutdown 的竞态收口（评审 C2）：入队后复查——若泳道恰在此刻
        # 死亡/收摊，_die 的排空可能已经跑完、本请求永远无人消费。此处补终态
        # 事件（若 _die 也失败过它，消费者读到首个终态即断开，双终态无害）。
        if self.dead or self._stop:
            out.put(("error", {"message": "批处理泳道不可用（dead/stopped）"}))
            return req_id, out
        self._ensure_thread()
        self._wake.set()
        return req_id, out

    def cancel(self, req_id: str) -> bool:
        """取消意图：驱动线程在下一步边界执行 remove。恒幂等。"""
        self._cancelled.add(req_id)
        self._wake.set()
        return True

    @property
    def active_count(self) -> int:
        return self._active_count

    @property
    def decoding_count(self) -> int:
        """真在 decode 批里共跑的行数（≠在册数：后者含引擎内排队，评审 C4）。"""
        return self._decoding_count

    @property
    def paused(self) -> bool:
        return self._paused

    def shutdown(self) -> None:
        self.dead = True                # 先摘牌：route_reason 即刻改判（评审 m23）
        self._stop = True
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=5.0)
            if t.is_alive():
                # 驱动线程还压在一个引擎步里（长预填可超 5s）：绝不从本线程去动
                # 它正在读写的结构（评审 M11）——它退出循环时自会 _teardown_rows
                print("[BatchLane] 驱动线程未在 5s 内退出，残留行由其退出路径收尾")
                return
        self._teardown_rows("批处理泳道已收摊")

    # ---- 驱动线程 ----

    def _ensure_thread(self) -> None:
        if self.dead or self._stop:
            return                      # 死泳道不再拉新驱动线程（评审 C2 从犯）
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="batch-lane", daemon=True)
                self._thread.start()

    def _fail_request(self, p: _Pending, msg: str) -> None:
        p.out.put(("error", {"message": msg}))

    def _drain_pending(self) -> List[_Pending]:
        got: List[_Pending] = []
        while True:
            try:
                got.append(self._pending.get_nowait())
            except queue.Empty:
                return got

    def _teardown_rows(self, msg: str) -> None:
        """给一切残留行/待处理请求响亮收尾（只许驱动线程或已确认无驱动线程时调）。"""
        for row in self._rows.values():
            row.out.put(("error", {"message": msg}))
        for p in self._holdback + self._drain_pending():
            self._fail_request(p, msg)
        self._rows.clear()
        self._req2uid.clear()
        self._holdback.clear()
        self._active_count = 0
        self._decoding_count = 0
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
            self._ctx = None

    def _run(self) -> None:
        # 整个循环体都在 try 里（评审 M12）：任何一处异常——包括 has_work/
        # interactive_busy 这类"读状态"调用——都必须走 _die，绝不允许驱动线程
        # 无声蒸发而泳道还挂着 armed 的招牌。
        try:
            while not self._stop:
                has_ctx_work = self._ctx is not None and self._ctx.has_work
                if (self._pending.empty() and not self._holdback
                        and not has_ctx_work and not self._cancelled):
                    self._paused = False
                    self._wake.wait(timeout=0.5)
                    self._wake.clear()
                    continue

                # 交互让路：单流路在生成 → 本泳道在引擎步边界整体停等。
                if self._interactive_busy():
                    self._paused = True
                    time.sleep(self._poll_s)
                    continue
                self._paused = False

                self._admit()
                self._apply_cancels()
                if self._ctx is not None and self._ctx.has_work:
                    self._step()
                self._active_count = len(self._rows)
                self._decoding_count = (self._ctx.decoding_count()
                                        if self._ctx is not None else 0)
        except Exception as e:
            # 引擎级异常：波及面是整代批（缓存状态不可信），全部行响亮失败，
            # 泳道永久 fail-closed（后续请求由 route_reason 的 armed 判定
            # 送回单流路）。绝不静默吞掉继续跑——那会产出看似正常的错误输出。
            self._die(f"批处理泳道引擎异常，泳道已下线：{type(e).__name__}: {e}")
            return
        # 正常收摊路径（_stop）：残留行同样必须见到终态事件
        self._teardown_rows("批处理泳道已收摊")

    def _die(self, msg: str) -> None:
        self.dead = True
        self._stop = True
        self._teardown_rows(msg)

    def _admit(self) -> None:
        """把待处理请求编入当前代生成器；采样参数不合的进等待区，
        泳道排空后重建生成器再收。"""
        incoming = self._holdback + self._drain_pending()
        self._holdback = []
        for p in incoming:
            if p.req_id in self._cancelled:
                self._cancelled.discard(p.req_id)
                p.out.put(("done", {"cancelled": True}))
                continue
            if len(self._rows) >= self.max_num_seqs:
                self._holdback.append(p)
                continue
            if self._ctx is None:
                self._ctx = self._build(p.skey)
                self._ctx_skey = p.skey
            elif p.skey != self._ctx_skey:
                if self._ctx.has_work or self._rows:
                    self._holdback.append(p)
                    continue
                self._ctx.close()
                self._ctx = self._build(p.skey)
                self._ctx_skey = p.skey
            self._insert(p)

    def _insert(self, p: _Pending) -> None:
        ctx = self._ctx
        input_ids = ctx.tokenize(p.prompt)
        prompt_kwargs = ctx.embed(input_ids)
        rep = p.sampling.get("repetition_penalty", 1.1)
        rep_ctx = int(p.sampling.get("repetition_context_size", 50))
        processors = ctx.row_processors(rep, rep_ctx)
        max_tokens = int(p.sampling.get("max_tokens", 4096))
        (uid,) = ctx.insert([input_ids], max_tokens=[max_tokens],
                            prompt_kwargs=[prompt_kwargs],
                            logits_processors=[processors])
        self._rows[uid] = _Row(req_id=p.req_id, out=p.out,
                               streamer=ctx.row_streamer(),
                               want_delta=p.want_delta,
                               prompt_tokens=len(input_ids))
        self._req2uid[p.req_id] = uid

    def _apply_cancels(self) -> None:
        if not self._cancelled:
            return
        for req_id in list(self._cancelled):
            uid = self._req2uid.get(req_id)
            if uid is not None:
                if not self._ctx.remove(uid):
                    # 行正被多行预填批处理，上游此刻摘不下来（remove 返回 False，
                    # 评审 M5）：意图保留，下一轮步边界重试——预填批一收就能摘。
                    # 绝不能假装摘掉：行会孤儿化继续吃 GPU 且逃出 max_num_seqs 记账。
                    continue
                self._cancelled.discard(req_id)
                row = self._rows.pop(uid, None)
                self._req2uid.pop(req_id, None)
                if row is not None:
                    row.out.put(("done", {"cancelled": True}))
                continue
            hb = [p for p in self._holdback if p.req_id == req_id]
            if hb:
                self._cancelled.discard(req_id)
                for p in hb:
                    self._holdback.remove(p)
                    p.out.put(("done", {"cancelled": True}))
                continue
            # 不在行也不在等待区：要么已结束（意图作废），要么还躺在 _pending
            # 队列里（本次 _admit 之后才投进来）。只有队列确认为空才可作废——
            # 否则留到下一轮由 _admit 消化，绝不静默丢失取消意图。
            if self._pending.empty():
                self._cancelled.discard(req_id)

    def _step(self) -> None:
        prompt_responses, responses = self._ctx.next()
        self.steps_total += 1
        now = time.perf_counter()
        for pr in prompt_responses:
            row = self._rows.get(pr.uid)
            if row is not None:
                row.prompt_tps = float(getattr(pr, "prompt_tps", 0.0) or 0.0)
        for r in responses:
            row = self._rows.get(r.uid)
            if row is None:
                continue
            tok = r.token
            finish = r.finish_reason
            if tok is None:
                text = row.streamer.finalize()
            else:
                tok = tok.item() if hasattr(tok, "item") else tok
                if row.t_first is None:
                    row.t_first = now
                row.n_tokens += 1
                text = row.streamer.advance(tok, finish)
            gen_tps = 0.0
            if row.t_first is not None and now > row.t_first and row.n_tokens > 1:
                # 首 token 归 prefill 侧，decode tps 分母从首 token 起算
                gen_tps = (row.n_tokens - 1) / (now - row.t_first)
            payload = {
                "text": text,
                "delta": (text or None) if row.want_delta else None,
                "prompt_tokens": row.prompt_tokens,
                "generation_tokens": row.n_tokens,
                "prompt_tps": row.prompt_tps,
                "generation_tps": gen_tps,
                "peak_memory": (self._ctx.peak_memory_gb() if finish else 0.0),
                "extra": {"batch_lane": True},
            }
            row.out.put(("chunk", payload))
            if finish is not None:
                row.out.put(("done", {"finish_reason": finish}))
                self._rows.pop(r.uid, None)
                self._req2uid.pop(row.req_id, None)


def build_upstream_context(backend, *, max_num_seqs: int):
    """生产工厂：返回 build_generator(skey) 闭包，包住上游 BatchGenerator。

    backend = 本服务已装载的 MLXBackend（权重共享，零双载）。所有 mlx/上游
    import 都在这里惰性发生——CPU-only 金测试永远不走本函数。
    本函数在装配期（单流路空闲）调用：detokenizer 模板也在此刻克隆，
    行级 streamer 从模板再克隆，不在生成中途读共享单例。
    """
    import copy as _copy

    import mlx.core as mx
    from mlx_vlm.generate.ar import BatchGenerator
    from mlx_vlm.sample_utils import make_logits_processors, make_sampler
    from mlx_vlm.tokenizer_utils import (_ServerTokenStreamer,
                                         make_streaming_detokenizer)

    model = backend.model
    # 私有 processor（Q14 首跑实撞，2026-08-23）：HF fast tokenizer 是 Rust
    # RefCell——单流路执行器线程与本泳道驱动线程共用同一实例会零星
    # `RuntimeError: Already borrowed`（撞点=驱动线程准入 tokenize/步末 detok
    # 与交互请求的 prompt encode 重叠）。装配期深拷一份：两个 Rust 对象无共享
    # 借用，热路零锁；权重（model）照旧共享，深拷只复制分词面，内存忽略不计。
    processor = _copy.deepcopy(backend.processor)
    tokenizer = processor.tokenizer if hasattr(processor, "tokenizer") else processor
    detok_template = make_streaming_detokenizer(processor)
    # add_special_tokens 按模型族条件（评审 M8）：与单流路/上游 _cpu_preprocess
    # 同一条逻辑——gemma 系无 chat_template 时不加（方言 prompt 自带全部标记），
    # 其余家族（qwen 等）由 tokenizer 补 BOS。写死 False 会让 qwen 档批路与
    # 单流路 prompt 首 token 不同，批不变性哨直接失真。
    _model_type = str(getattr(getattr(model, "config", None), "model_type", ""))
    add_special = (getattr(processor, "chat_template", None) is None
                   if _model_type in ("gemma3", "gemma3n", "gemma4",
                                      "gemma4_unified")
                   else True)

    class _Ctx:
        def __init__(self, skey: Tuple[float, float]):
            temp, min_p = skey
            sampler = None if temp == 0 else make_sampler(temp=temp, min_p=min_p)
            kv_kwargs = {}
            if backend.kv_bits is not None:
                kv_kwargs = {"kv_bits": backend.kv_bits,
                             "kv_quant_scheme": backend.kv_quant_scheme,
                             "quantized_kv_start": backend.quantized_kv_start}
            # stream 不传：BatchGenerator 缺省的 generation_stream 是
            # ThreadLocalStream，驱动线程自动分得私流（见模块头 ⚠️）。
            # prefill_batch_size=1：准入门语义见模块头（评审 C3，不许改回）。
            self._gen = BatchGenerator(
                model.language_model, processor,
                sampler=sampler,
                greedy_sampling=(temp == 0),
                completion_batch_size=max_num_seqs,
                prefill_batch_size=1,
                compute_logprobs=False,
                **kv_kwargs,
            )

        # ---- BatchLane 消费面 ----
        def tokenize(self, prompt: str) -> List[int]:
            return tokenizer.encode(prompt, add_special_tokens=add_special)

        def embed(self, input_ids: List[int]) -> Dict[str, Any]:
            # 上游契约：BatchGenerator 行恒带 inputs_embeds（text-only 也过
            # get_input_embeddings，与上游 server._gpu_embed 同口径——彼处也
            # 不包 stream 上下文，走当前线程缺省流）
            ids = mx.array([input_ids])
            embed = model.get_input_embeddings(ids, None)
            return {k: v for k, v in embed.to_dict().items() if v is not None}

        def row_streamer(self):
            d = _copy.copy(detok_template)
            d.reset()
            return _ServerTokenStreamer(tokenizer, d)

        def row_processors(self, rep, rep_ctx):
            if rep is None:
                return []
            return make_logits_processors(None, float(rep), int(rep_ctx))

        def peak_memory_gb(self) -> float:
            return mx.get_peak_memory() / 1e9

        def decoding_count(self) -> int:
            return len(self._gen._generation_batch)

        def insert(self, *a, **kw):
            return self._gen.insert(*a, **kw)

        def next(self):
            return self._gen.next()

        def remove(self, uid) -> bool:
            return bool(self._gen.remove(uid))

        @property
        def has_work(self):
            return self._gen.has_work

        def close(self):
            self._gen.close()
            mx.clear_cache()

    return _Ctx
