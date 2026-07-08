"""工具调用段约束采样（inference-speedup Phase 2）。

只约束 <|tool_call> … <tool_call|> 区间：命中起始标记后挂上 llguidance 的
JSON-schema 状态机做 logits mask，JSON 完成即只放行收尾标记；自由文本/思考段
完全放开。目标是把「生成→解析失败→纠错反馈→重生成」的废轮清零。

设计要点：
- fail-open：llguidance 缺失、语法编译失败、运行期任何异常 → 记录告警并
  放弃约束（本次生成退回自由采样），既有 parser 纠错闭环仍在兜底；
- 单例复用：LLTokenizer 构建 ~1s，进程内建一次；语法按工具集缓存；
  matcher 借推理互斥锁的串行性全程复用（每次生成 reset）；
- prompt 边界：processor 首次被调用时的历史全量视为 prompt，不进状态机
  （历史里回放的旧工具调用不会误触发）；
- mask 语义与 mlx_vlm.generate_step 的 logits_processors 协议对齐：
  (tokens_history, logits) -> logits，增量消费历史、返回掩码后的 logits。
"""
from typing import Any, Dict, List, Optional

import mlx.core as mx
import numpy as np

from . import events

try:
    import llguidance
    from llguidance import hf as _llhf
except Exception:  # pragma: no cover - 环境缺 llguidance 时整体禁用
    llguidance = None
    _llhf = None

_NEG_INF = float("-inf")


def build_tool_call_schema(tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把注册工具的声明编成调用体 JSON schema（anyOf 按工具分支）。

    锚定 prompt_builder 回放给模型看的格式：
    {"type": "function", "name": <tool>, "arguments": {…}}（type 可省略）。
    """
    branches = []
    for t in tools:
        params = t.get("parameters") or {}
        arg_schema: Dict[str, Any] = {"type": "object"}
        props = params.get("properties") or {}
        if props:
            # 剥掉与 JSON 校验无关的键（description 等保留无害，但裁剪可缩小语法）
            arg_schema["properties"] = props
        required = params.get("required") or []
        if required:
            arg_schema["required"] = list(required)
        arg_schema["additionalProperties"] = False
        branches.append({
            "type": "object",
            "properties": {
                "type": {"const": "function"},
                "name": {"const": t.get("name")},
                "arguments": arg_schema,
            },
            "required": ["name", "arguments"],
            "additionalProperties": False,
        })
    return {"anyOf": branches} if branches else {"type": "object"}


class ToolConstraintEngine:
    """进程级单例部件：LLTokenizer + 按工具集缓存的语法/matcher。"""

    def __init__(self, hf_tokenizer, stc_id: int, etc_id: int):
        self._ltok = _llhf.from_tokenizer(hf_tokenizer)
        self.vocab_size = self._ltok.vocab_size
        self.stc_id = stc_id
        self.etc_id = etc_id
        self._matchers: Dict[tuple, Any] = {}  # 工具名元组 -> LLMatcher（复用，reset 后重入）

    def matcher_for(self, tools: List[Dict[str, Any]]):
        key = tuple(sorted(t.get("name", "") for t in tools))
        m = self._matchers.get(key)
        if m is not None:
            m.reset()
            # 停机态（如 ParserTooComplex）不随 reset 恢复：继续复用会让之后每次
            # 工具调用的 bitmask 全空 → 全 -inf 采样出垃圾 token，把模型带崩
            # （实测：一次超长调用撑爆后，同进程全部后续调用被毁）。弃用重建。
            if m.is_error():
                print(f"[Constrained] 缓存 matcher 处于不可恢复错误态，重建: {m.get_error()}")
                events.emit("matcher_rebuild", error=m.get_error())
                m = None
        if m is None:
            grammar = llguidance.LLMatcher.grammar_from_json_schema(
                build_tool_call_schema(tools),
                defaults={"whitespace_flexible": True},
            )
            m = llguidance.LLMatcher(self._ltok, grammar)
            if m.is_error():
                raise RuntimeError(f"llguidance grammar error: {m.get_error()}")
            self._matchers[key] = m
        return m


class ToolCallGuard:
    """单次生成的 logits processor（状态机）。

    IDLE：透传 logits；消费到 stc 标记 → ACTIVE。
    ACTIVE：llguidance bitmask 掩码；JSON 到达可接受态 → 只放行 etc → IDLE。
    任何异常 → fail-open（self.failed 置位，之后全程透传）。
    """

    def __init__(self, matcher, stc_id: int, etc_id: int, vocab_size: int):
        self._m = matcher
        self._stc = stc_id
        self._etc = etc_id
        self._vocab = vocab_size
        self._seen: Optional[int] = None   # 已消费的历史长度；首调用置为 prompt 边界
        self.active = False
        self.failed = False
        self.masked_positions = 0          # 仪表：被约束的采样步数

    # ---- mlx_vlm logits_processors 协议 ----
    def __call__(self, tokens: mx.array, logits: mx.array) -> mx.array:
        if self.failed or llguidance is None:
            return logits
        try:
            n = int(tokens.size)
            if self._seen is None:
                self._seen = n            # prompt 边界：既往历史不进状态机
                return logits
            if n > self._seen:
                for t in tokens[self._seen:n].tolist():
                    self._consume(int(t))
                self._seen = n
            return self._mask(logits)
        except Exception as e:  # fail-open：绝不让约束器炸掉一轮生成
            self._fail_open(e)
            return logits

    # ---- 供 Phase 3 投机走查直接驱动（绕过增量历史协议）----
    def consume(self, token_id: int) -> None:
        if not self.failed:
            try:
                self._consume(token_id)
                if self._seen is not None:
                    self._seen += 1
            except Exception as e:
                self._fail_open(e)

    def mask_logits(self, logits: mx.array) -> mx.array:
        if self.failed:
            return logits
        try:
            return self._mask(logits)
        except Exception as e:
            self._fail_open(e)
            return logits

    def _fail_open(self, e: Exception) -> None:
        print(f"[Constrained] fail-open: {type(e).__name__}: {e}")
        events.emit("constrained_failopen", error=f"{type(e).__name__}: {e}",
                    masked_positions=self.masked_positions)
        self.failed = True
        self.active = False

    # ---- 内部 ----
    def _consume(self, t: int) -> None:
        if self.active:
            if t == self._etc:
                self.active = False        # 收尾标记：调用体结束
            else:
                ok = self._m.consume_token(t)
                if ok is False or self._m.is_error():
                    # 理论上被 mask 挡住不会走到这；到了就 fail-open
                    raise RuntimeError(self._m.get_error() or "consume rejected")
        elif t == self._stc:
            self._m.reset()
            self.active = True

    def _mask(self, logits: mx.array) -> mx.array:
        if not self.active:
            return logits
        self.masked_positions += 1
        if self._m.is_accepting():
            # JSON 已完整：只放行收尾标记，强制立刻闭合（不给模型画蛇添足的机会）
            neg = mx.full((), _NEG_INF, dtype=logits.dtype)
            return mx.where(mx.arange(logits.shape[-1]) == self._etc, logits, neg)
        bm = self._m.compute_bitmask()
        bits = np.unpackbits(np.frombuffer(bm, dtype=np.uint8), bitorder="little")
        allowed = bits[: logits.shape[-1]]
        if not allowed.any():
            # 空 bitmask（matcher 卡死/停机）：全 -inf 会让采样退化成掷骰子、吐出
            # 垃圾 token 把模型带偏。宁可整次生成 fail-open 放开约束。
            raise RuntimeError("empty bitmask: matcher allows no token (stuck/stopped state)")
        mask = mx.array(allowed.astype(bool))
        neg = mx.full((), _NEG_INF, dtype=logits.dtype)
        return mx.where(mask, logits, neg)
