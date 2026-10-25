"""§7-0① 批处理泳道金测试（CPU-only：假生成器驱动全部状态机）。

守的不变量：
- route_reason fail-closed：任何单流路专属特性在场即拒进泳道——批路静默忽略
  guard/缓存/思考/投机 = 把约束悄悄丢掉（§S.3 同族），路由判定是这条红线的唯一闸口；
- 采样缺省与单流路逐字对齐（temp 0.2 / min_p 0.05）；temp=0 时 min_p 归一
  （贪心路死参数不分代）；
- 连续批处理语义：行可中途加入、逐行退出、逐行独立 streamer；
- 交互优先：interactive_busy 为真时驱动线程在步边界停等（一个 next 都不走）；
- 采样参数分代：不同 (temp, min_p) 的请求等泳道排空后重建生成器，不混批；
- 引擎异常 → 全部行响亮失败 + 泳道永久 fail-closed（dead），绝不静默续跑；
- remove 返回 False（行正被多行预填）→ 取消意图保留重试，绝不假装摘掉（评审 M5）；
- 背压：在途超限 submit 拒收（server 退单流路），不无界攒等待者（评审 M13）；
- shutdown / cancel / submit-与-die 竞态都不留挂死的消费者（输出队列必见终态事件）。
"""
import importlib.util
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_MOD_PATH = Path(__file__).resolve().parents[2] / "inference" / "batch_lane.py"
_spec = importlib.util.spec_from_file_location("batch_lane_under_test", _MOD_PATH)
bl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bl)


# ---------- 假生成器（上游 BatchGenerator 协议的最小可编排替身） ----------

class FakeStreamer:
    def advance(self, tok, finish):
        return f"<{tok}>"

    def finalize(self):
        return "[fin]"


class FakeCtx:
    """每行每步吐一个 token，吐满 tokens_per_row 步后带 finish_reason 结束。"""

    def __init__(self, skey, *, tokens_per_row=3, fail_at_step=None,
                 final_none_token=False, remove_fail_times=0):
        self.skey = skey
        self.tokens_per_row = tokens_per_row
        self.fail_at_step = fail_at_step
        self.final_none_token = final_none_token
        self.remove_fail_times = remove_fail_times
        self.rows = {}
        self.uid = 0
        self.steps = 0
        self.closed = False
        self.inserted_processors = []

    def tokenize(self, prompt):
        return list(range(max(1, len(prompt))))

    def embed(self, input_ids):
        return {"inputs_embeds": f"E{len(input_ids)}"}

    def row_streamer(self):
        return FakeStreamer()

    def row_processors(self, rep, rep_ctx):
        return [] if rep is None else [("rep", rep, rep_ctx)]

    def peak_memory_gb(self):
        return 1.5

    def decoding_count(self):
        return sum(1 for r in self.rows.values() if not r.fresh)

    def insert(self, prompts, max_tokens, prompt_kwargs, logits_processors):
        uids = []
        for p, m, lp in zip(prompts, max_tokens, logits_processors):
            self.rows[self.uid] = SimpleNamespace(emitted=0, fresh=True,
                                                  budget=min(m, self.tokens_per_row))
            self.inserted_processors.append(lp)
            uids.append(self.uid)
            self.uid += 1
        return uids

    def next(self):
        self.steps += 1
        if self.fail_at_step is not None and self.steps >= self.fail_at_step:
            raise RuntimeError("boom")
        prompt_rs = []
        for uid, row in self.rows.items():
            if row.fresh:
                row.fresh = False
                prompt_rs.append(SimpleNamespace(uid=uid, prompt_tps=123.0))
        resp = []
        for uid, row in list(self.rows.items()):
            row.emitted += 1
            done = row.emitted >= row.budget
            if done and self.final_none_token:
                resp.append(SimpleNamespace(uid=uid, token=None,
                                            finish_reason="stop"))
            else:
                resp.append(SimpleNamespace(uid=uid, token=row.emitted,
                                            finish_reason="stop" if done else None))
            if done:
                del self.rows[uid]
        return prompt_rs, resp

    def remove(self, uid):
        if self.remove_fail_times > 0:
            self.remove_fail_times -= 1
            return False        # 上游语义：行正被多行预填批处理，暂时摘不下来
        return self.rows.pop(uid, None) is not None

    @property
    def has_work(self):
        return bool(self.rows)

    def close(self):
        self.closed = True


def make_lane(*, max_num_seqs=4, busy=None, **ctx_kw):
    built = []

    def build(skey):
        ctx = FakeCtx(skey, **ctx_kw)
        built.append(ctx)
        return ctx

    flag = busy if busy is not None else (lambda: False)
    lane = bl.BatchLane(build_generator=build, max_num_seqs=max_num_seqs,
                        interactive_busy=flag, poll_s=0.001)
    return lane, built


def drain(q, timeout=5.0):
    """收完一个请求的全部事件（直到 done/error），带超时防挂死。"""
    events, deadline = [], time.time() + timeout
    while time.time() < deadline:
        try:
            kind, payload = q.get(timeout=0.05)
        except queue.Empty:
            continue
        events.append((kind, payload))
        if kind in ("done", "error"):
            return events
    raise AssertionError(f"事件流超时未见终态：{events}")


# ---------- route_reason：fail-closed 路由矩阵 ----------

def _body(**kw):
    features = kw.pop("features", {})
    return {"prompt": "hi", "features": {"batch_ok": True, **features}, **kw}


def test_route_requires_explicit_opt_in():
    assert bl.route_reason({"features": {}}, armed=True,
                           has_speculative=False) == bl.REASON_NOT_OPTED


def test_route_refuses_when_not_armed():
    assert bl.route_reason(_body(), armed=False,
                           has_speculative=False) == bl.REASON_NOT_ARMED


def test_route_refuses_speculative_backend():
    """drafter **或** prompt-lookup 在场都拒：批路静默退非投机 = 分母污染。"""
    assert bl.route_reason(_body(), armed=True,
                           has_speculative=True) == bl.REASON_SPECULATIVE


@pytest.mark.parametrize("body,expected", [
    (_body(images=["m1"]), bl.REASON_MEDIA),
    (_body(audios=["a1"]), bl.REASON_MEDIA),
    (_body(features={"tool_specs": [{"name": "t"}]}), bl.REASON_TOOL_GUARD),
    (_body(features={"thinking_budget": 512}), bl.REASON_THINKING),
    (_body(features={"enable_thinking": True}), bl.REASON_THINKING),
    (_body(cache_id="c1"), bl.REASON_SESSION_CACHE),
    (_body(features={"prompt_delta_offset": 10}), bl.REASON_DELTA_FEED),
])
def test_route_refuses_single_lane_features(body, expected):
    assert bl.route_reason(body, armed=True, has_speculative=False) == expected


def test_route_admits_clean_text_request():
    assert bl.route_reason(_body(), armed=True, has_speculative=False) == ""


def test_thinking_budget_zero_still_refused():
    """0 也是显式给了预算（歧义值按拒处理——fail-closed 判 is not None）。"""
    body = _body(features={"thinking_budget": 0})
    assert bl.route_reason(body, armed=True,
                           has_speculative=False) == bl.REASON_THINKING


def test_sampling_key_defaults_match_single_lane():
    """缺省 (0.2, 0.05) 与 MLXBackend.stream_generate 的缺省逐字对齐；
    temp=0 时 min_p 归一为 0（贪心路死参数，不许无谓分代）。"""
    assert bl.sampling_key({}) == (0.2, 0.05)
    assert bl.sampling_key({"temperature": 0}) == (0.0, 0.0)
    assert bl.sampling_key({"temperature": 0, "min_p": 0.05}) == (0.0, 0.0)


# ---------- 生命周期 ----------

def test_single_request_streams_and_finishes():
    lane, built = make_lane()
    try:
        _, q = lane.submit("hello", {"temperature": 0, "max_tokens": 8})
        events = drain(q)
        kinds = [k for k, _ in events]
        assert kinds[-1] == "done" and kinds.count("chunk") == 3
        first = events[0][1]
        assert set(first) == {"text", "delta", "prompt_tokens",
                              "generation_tokens", "prompt_tps",
                              "generation_tps", "peak_memory", "extra"}
        assert first["extra"] == {"batch_lane": True}
        assert first["prompt_tps"] == 123.0
        assert first["prompt_tokens"] == 5      # len("hello")
        last_chunk = [p for k, p in events if k == "chunk"][-1]
        assert last_chunk["generation_tokens"] == 3
        assert last_chunk["peak_memory"] == 1.5  # 终态才报（与单流路同口径）
        assert events[-1][1] == {"finish_reason": "stop"}
        assert built[0].skey == (0.0, 0.0)
    finally:
        lane.shutdown()


def test_want_delta_false_suppresses_delta():
    lane, _ = make_lane()
    try:
        _, q = lane.submit("hi", {"temperature": 0}, want_delta=False)
        events = drain(q)
        chunks = [p for k, p in events if k == "chunk"]
        assert chunks and all(p["delta"] is None for p in chunks)
        assert all(p["text"] for p in chunks)   # text 照常给（单流路同义）
    finally:
        lane.shutdown()


def test_two_rows_share_one_generation():
    """第二行中途加入同一代生成器（连续批处理），两行都正常完结。"""
    lane, built = make_lane(tokens_per_row=6)
    try:
        _, q1 = lane.submit("aaaa", {"temperature": 0})
        # 等第一行真的开跑（steps>0）再投第二行，确保是"中途加入"
        deadline = time.time() + 5
        while built and built[0].steps == 0 and time.time() < deadline:
            time.sleep(0.002)
        _, q2 = lane.submit("bbbb", {"temperature": 0})
        e1, e2 = drain(q1), drain(q2)
        assert e1[-1][0] == "done" and e2[-1][0] == "done"
        assert len(built) == 1          # 同参数不重建生成器
    finally:
        lane.shutdown()


def test_final_none_token_takes_finalize_path():
    lane, _ = make_lane(final_none_token=True, tokens_per_row=2)
    try:
        _, q = lane.submit("xy", {"temperature": 0})
        events = drain(q)
        chunks = [p for k, p in events if k == "chunk"]
        assert chunks[-1]["text"] == "[fin]"    # finalize 冲刷段
    finally:
        lane.shutdown()


def test_cancel_mid_decode():
    # 行预算必须实际无限（tokens_per_row 与 max_tokens 双封顶）：假引擎热循环
    # ~0.1s 能吃完 4096 步，预算有限时取消会偶发输给自然完结（全套件负载下实翻）
    lane, built = make_lane(tokens_per_row=10**9)
    try:
        req_id, q = lane.submit("zz", {"temperature": 0, "max_tokens": 10**9})
        kind, _ = q.get(timeout=5)      # 至少收到一个 chunk（行已在跑）
        assert kind == "chunk"
        lane.cancel(req_id)
        events = drain(q)
        assert events[-1] == ("done", {"cancelled": True})
        deadline = time.time() + 5
        while built[0].rows and time.time() < deadline:
            time.sleep(0.002)
        assert not built[0].rows        # 行已从引擎摘除
    finally:
        lane.shutdown()


def test_cancel_retries_when_engine_refuses_remove():
    """上游 remove=False（行在多行预填批里）：意图保留、下一轮重试成功，
    绝不假装摘掉留孤儿行（评审 M5）。"""
    lane, built = make_lane(tokens_per_row=10**9, remove_fail_times=2)
    try:
        req_id, q = lane.submit("zz", {"temperature": 0, "max_tokens": 10**9})
        q.get(timeout=5)                # 行已在跑
        lane.cancel(req_id)
        events = drain(q)
        assert events[-1] == ("done", {"cancelled": True})
        assert built[0].remove_fail_times == 0   # False 的两次都被消耗=真的重试了
        deadline = time.time() + 5
        while built[0].rows and time.time() < deadline:
            time.sleep(0.002)
        assert not built[0].rows
    finally:
        lane.shutdown()


def test_interactive_busy_pauses_stepping():
    busy = {"flag": True}
    lane, built = make_lane(busy=lambda: busy["flag"], tokens_per_row=3)
    try:
        _, q = lane.submit("pp", {"temperature": 0})
        time.sleep(0.15)
        assert built == [] or built[0].steps == 0   # 让路期间一步都不走
        assert lane.paused
        busy["flag"] = False
        events = drain(q)
        assert events[-1][0] == "done"
    finally:
        lane.shutdown()


def test_different_sampling_waits_for_rebuild():
    lane, built = make_lane(tokens_per_row=4)
    try:
        _, q1 = lane.submit("aa", {"temperature": 0})
        _, q2 = lane.submit("bb", {"temperature": 0.7})
        e1, e2 = drain(q1), drain(q2)
        assert e1[-1][0] == "done" and e2[-1][0] == "done"
        assert [c.skey for c in built] == [(0.0, 0.0), (0.7, 0.05)]
        assert built[0].closed          # 排空后旧代被关闭再重建
    finally:
        lane.shutdown()


def test_max_num_seqs_caps_admission():
    lane, built = make_lane(max_num_seqs=2, tokens_per_row=3)
    try:
        qs = [lane.submit(p, {"temperature": 0})[1] for p in ("a", "b", "c")]
        for q in qs:
            assert drain(q)[-1][0] == "done"
        assert len(built) == 1
        assert built[0].uid == 3        # 三行全走完（第三行等到空位）
    finally:
        lane.shutdown()


def test_backpressure_rejects_when_inflight_full():
    """在途超限（max_inflight）→ submit 拒收（server 退单流路），不无界攒队。"""
    busy = {"flag": True}               # 按住驱动线程：所有请求都堆在 pending
    lane, _ = make_lane(busy=lambda: busy["flag"])
    try:
        for i in range(lane.max_inflight):
            lane.submit(f"p{i}", {"temperature": 0})
        with pytest.raises(RuntimeError, match="在途已满"):
            lane.submit("overflow", {"temperature": 0})
    finally:
        lane.shutdown()


def test_engine_exception_fails_all_rows_and_kills_lane():
    busy = {"flag": True}               # 先按住驱动线程，两单都投进去再放行
    lane, _ = make_lane(tokens_per_row=100, fail_at_step=2,
                        busy=lambda: busy["flag"])
    try:
        _, q1 = lane.submit("a", {"temperature": 0})
        _, q2 = lane.submit("b", {"temperature": 0})
        busy["flag"] = False
        e1, e2 = drain(q1), drain(q2)
        assert e1[-1][0] == "error" and e2[-1][0] == "error"
        assert "泳道已下线" in e1[-1][1]["message"]
        assert lane.dead
        with pytest.raises(RuntimeError):
            lane.submit("c", {"temperature": 0})
    finally:
        lane.shutdown()


def test_submit_racing_die_still_gets_terminal_event():
    """submit 的 dead 前置检查通过后泳道恰好死亡：入队后的复查必须补终态事件，
    消费者绝不能无限挂在 out_q.get 上（评审 C2）。"""
    lane, _ = make_lane()
    orig_put = lane._pending.put

    def put_then_die(p):
        orig_put(p)
        # 模拟 _die 恰在入队后发生：排空 pending（本请求被清走但 _die 的
        # fail 发生在 submit 复查之前的另一个世界线——这里直接吃掉它，
        # 只留 submit 自己的复查来兜底）
        lane._pending.get_nowait()
        lane.dead = True
        lane._stop = True

    lane._pending.put = put_then_die
    _, q = lane.submit("x", {"temperature": 0})
    events = drain(q, timeout=2)
    assert events[-1][0] == "error"


def test_shutdown_notifies_waiting_consumers():
    busy = {"flag": True}               # 永久让路：请求进得来、跑不动
    lane, _ = make_lane(busy=lambda: busy["flag"])
    _, q = lane.submit("a", {"temperature": 0})
    lane.shutdown()
    events = drain(q)
    assert events[-1][0] == "error" and "收摊" in events[-1][1]["message"]
    assert lane.dead                    # 收摊即摘牌（route_reason 即刻改判）
    with pytest.raises(RuntimeError):
        lane.submit("late", {"temperature": 0})


def test_row_processors_carry_repetition_penalty():
    lane, built = make_lane()
    try:
        _, q = lane.submit("a", {"temperature": 0})          # 缺省 rep 1.1/50
        drain(q)
        _, q = lane.submit("b", {"temperature": 0, "repetition_penalty": None})
        drain(q)
        assert built[0].inserted_processors == [[("rep", 1.1, 50)], []]
    finally:
        lane.shutdown()


def test_decoding_count_gauge_distinct_from_active():
    """decoding_count 数的是真在 decode 批里的行（≠在册数，评审 C4——
    在册含引擎内排队，拿它当"同批共跑"证据是假证）。"""
    busy = {"flag": True}
    lane, built = make_lane(busy=lambda: busy["flag"], tokens_per_row=100_000)
    try:
        # max_tokens 必须显式给：泳道缺省 4096 会把 tokens_per_row 截掉，快机上两行
        # 在轮询看到之前就已跑完（假引擎每步零开销），断言就成了赌调度
        lane.submit("a", {"temperature": 0, "max_tokens": 100_000})
        lane.submit("b", {"temperature": 0, "max_tokens": 100_000})
        time.sleep(0.05)
        assert lane.decoding_count == 0     # 还没开跑
        busy["flag"] = False
        deadline = time.time() + 5
        while lane.decoding_count < 2 and time.time() < deadline:
            time.sleep(0.002)
        assert lane.decoding_count == 2     # 两行真的同批共跑
    finally:
        lane.shutdown()


# ---------- 源码锚（生产工厂 + server 接线，与 test_entry_speculative 同法） ----------

def test_upstream_factory_source_anchors():
    src = _MOD_PATH.read_text(encoding="utf-8")
    # 准入门语义（评审 C3）：completion==prefill 时行只能在批排空后加入=静默串行。
    # 谁想"优化"回去，先读上游 ar.py _next 的 num_to_add >= prefill_batch_size。
    assert "prefill_batch_size=1" in src
    # 线程绑定流的教训（首轮真机冒烟实炸）：不许出现 mx.new_stream
    assert "mx.new_stream(" not in src.replace("不得换成 mx.new_stream", "")
    # 行级 detokenizer 从模板克隆，不在生成中途读共享单例
    assert "make_streaming_detokenizer(processor)" in src
    # add_special_tokens 按模型族条件（评审 M8），不许写死
    assert '"gemma4_unified"' in src
    # 泳道 processor 必须是私有深拷（Q14 首跑实撞 `Already borrowed`：
    # HF fast tokenizer 的 Rust RefCell 不许跨单流/批两线程共用一实例）
    assert "_copy.deepcopy(backend.processor)" in src
    assert "processor = backend.processor" not in src
    # 修复前原文是 `model, processor = backend.model, backend.processor`——
    # 上一条负锚匹配不到它（恒真）；真正挡住退回的是这一条（复审 m8）
    assert "model, processor = backend.model, backend.processor" not in src


def test_server_wiring_anchors():
    server = (_MOD_PATH.parent / "server.py").read_text(encoding="utf-8")
    assert "INFER_MLX_MAX_CONCURRENCY" in server           # env 覆盖（测量场）
    assert 'backend_name in ("", "mlx") and _mlx_n > 1' in server  # 只对 MLX 武装
    assert "def _busy_executor_call" in server             # 让路标记的统一托管
    # GPU 工作必须对批泳道可见（评审 M7）：转写 + 缓存三件套 + 门面非流式
    assert server.count("_busy_executor_call(") >= 6       # def + 5 个调用点
    assert server.count("S.single_busy += 1") == 3         # 两条 SSE 生成路 + 托管助手
    assert "has_speculative=_spec_on" in server            # 投机（含 lookup）拒进泳道
    assert "S.batch_lane.cancel(lane_req)" in server       # 取消端点接到泳道
    assert "S.batch_lane.shutdown()" in server             # 收摊次序：先泳道后后端
    assert "keepalive" in server                           # 让路窗口的 SSE 保活
    assert "set_wired_limit" in server                     # 嵌套 wired_limit 幂等化
