"""工具调用段约束采样（inference-speedup Phase 2）——纯逻辑层。

只约束 <|tool_call> … <tool_call|> 区间：命中起始标记后挂上 llguidance 的
JSON-schema 状态机做 logits mask，JSON 完成即只放行收尾标记；自由文本/思考段
完全放开。目标是把「生成→解析失败→纠错反馈→重生成」的废轮清零。

设计要点：
- fail-open：llguidance 缺失、语法编译失败、运行期任何异常 → 记录告警并
  放弃约束（本次生成退回自由采样），既有 parser 纠错闭环仍在兜底；
- 单例复用：LLTokenizer 构建 ~1s，进程内建一次；语法按工具集缓存；
  matcher 借推理互斥锁的串行性全程复用（每次生成 reset）；
- 框架无关（P0 收编）：本模块只做状态机与 bitmask 决策（numpy），不碰任何
  张量库。数组端的 mask 应用由各推理后端的适配器完成（如 MLX 后端的
  _GuardAdapter），prompt 边界（既往历史不进状态机）与增量消费协议也由
  适配器维护。
"""
from typing import Any, Dict, List, Optional, Union

import numpy as np

from . import events

try:
    import llguidance
    from llguidance import hf as _llhf
except Exception:  # pragma: no cover - 环境缺 llguidance 时整体禁用
    llguidance = None
    _llhf = None


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
    """单次生成的约束状态机（纯逻辑，不做数组运算）。

    IDLE：不约束；消费到 stc 标记 → ACTIVE。
    ACTIVE：llguidance bitmask 决策；JSON 到达可接受态 → 只放行 etc → IDLE。
    任何异常 → fail-open（self.failed 置位，之后全程放开）。

    后端适配器的消费协议：
    - consume(token_id)：每个已产出 token 逐个喂入（内部 fail-open）；
    - next_allowed()：本步采样约束——None=不约束；int=只放行该 token id；
      np.ndarray(bool)=allowed 集。异常上抛，由适配器调 fail_open 放开。
    """

    def __init__(self, matcher, stc_id: int, etc_id: int, vocab_size: int):
        self._m = matcher
        self._stc = stc_id
        self._etc = etc_id
        self._vocab = vocab_size
        self.active = False
        self.failed = False
        self.masked_positions = 0          # 仪表：被约束的采样步数

    def consume(self, token_id: int) -> None:
        if self.failed or llguidance is None:
            return
        try:
            self._consume(int(token_id))
        except Exception as e:
            self.fail_open(e)

    def next_allowed(self) -> Union[None, int, np.ndarray]:
        if self.failed or llguidance is None or not self.active:
            return None
        self.masked_positions += 1
        if self._m.is_accepting():
            # JSON 已完整：只放行收尾标记，强制立刻闭合（不给模型画蛇添足的机会）
            return self._etc
        bm = self._m.compute_bitmask()
        bits = np.unpackbits(np.frombuffer(bm, dtype=np.uint8), bitorder="little")
        allowed = bits[: self._vocab]
        if not allowed.any():
            # 空 bitmask（matcher 卡死/停机）：全 -inf 会让采样退化成掷骰子、吐出
            # 垃圾 token 把模型带偏。宁可整次生成 fail-open 放开约束。
            raise RuntimeError("empty bitmask: matcher allows no token (stuck/stopped state)")
        return allowed.astype(bool)

    def fail_open(self, e: Exception) -> None:
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
