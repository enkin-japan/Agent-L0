"""工具调用段约束采样（inference-speedup Phase 2）——纯逻辑层。

只约束 <|tool_call> … <tool_call|> 区间：命中起始标记后挂上 llguidance 的
语法状态机做 logits mask，调用体完成即只放行收尾标记；自由文本/思考段
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
- 方言无关（R0 收编 / §7-8 拓宽）：调用体的**语法**与起止标记 id 是**方言事实**，
  由调用方（AgentEngine）从 ModelDialect 取来喂进本模块——本模块只认「起止标记
  夹一段调用体」这个通用形状，按 ToolCallGrammar.kind 选 llguidance 的编译入口，
  不解读语法内容。语法体住在方言里（gemma-4 见 dialects/gemma4/tool_grammar.py，
  它 docstring 自陈锚定 prompt_builder 的回放格式）。
  §7-8 之前这里写死 JSON schema：gemma-4 迁到官方紧凑 DSL 后调用体不再是 JSON
  （裸键、`<|"|>` 定界的原文串），schema 表达不了，故改为按 kind 分派。
"""
import hashlib
import json
from typing import Any, Callable, Dict, List, Optional, Union

import numpy as np

from . import events

try:
    import llguidance
    from llguidance import hf as _llhf
except Exception:  # pragma: no cover - 环境缺 llguidance 时整体禁用
    llguidance = None
    _llhf = None


class ToolConstraintEngine:
    """进程级单例部件：LLTokenizer + 按工具集缓存的语法/matcher。

    grammar_builder（R0/§7-8）：`tools -> ToolCallGrammar`，由方言提供
    （ModelDialect.tool_call_grammar）。本模块不解读语法体，只按 kind 选编译入口。
    """

    def __init__(self, hf_tokenizer, stc_id: int, etc_id: int,
                 grammar_builder: Callable[[List[Dict[str, Any]]], Any]):
        self._ltok = _llhf.from_tokenizer(hf_tokenizer)
        self.vocab_size = self._ltok.vocab_size
        self.stc_id = stc_id
        self.etc_id = etc_id
        self._grammar_builder = grammar_builder
        self._matchers: Dict[str, Any] = {}  # 声明内容摘要 -> LLMatcher（复用，reset 后重入）

    @staticmethod
    def _tools_key(tools: List[Dict[str, Any]]) -> str:
        """缓存键取工具声明的**内容**摘要，不是名字集合。

        名字做键会在「同名不同 schema」时静默命中错的语法。这个组合自 §6.5 解耦后
        才真实存在：catalog 模式下 prompt 面的瘦目录与语法面的完整 schema 名字完全
        相同。进程内只有完整 schema 会走到这里，但 `inference/server.py` 的同款缓存
        是**跨客户端共享**的——版本歪斜的旧客户端捎带瘦目录，就会毒化后续所有客户端
        的语法，且既不报错也不 fail-open（拿到的是一份合法但错误的语法）。

        逐条规范化后**排序**再摘要：既对内容敏感，又保持旧键（名字集合）的顺序无关性，
        免得声明顺序一变就白白重编译一份等价语法。
        """
        blobs = sorted(json.dumps(t, sort_keys=True, ensure_ascii=False, default=str)
                       for t in tools)
        return hashlib.sha256("\n".join(blobs).encode("utf-8")).hexdigest()

    def matcher_for(self, tools: List[Dict[str, Any]]):
        key = self._tools_key(tools)
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
            spec = self._grammar_builder(tools)
            kind = getattr(spec, "kind", "json_schema")
            if kind == "lark":
                grammar = llguidance.LLMatcher.grammar_from_lark(spec.body)
            elif kind == "json_schema":
                grammar = llguidance.LLMatcher.grammar_from_json_schema(
                    getattr(spec, "body", spec),
                    defaults={"whitespace_flexible": True},
                )
            else:
                # 未知语法形态：抛出而非猜测——调用方 fail-open 到自由采样 +
                # 解析纠错闭环，好过拿错语法把模型约束成畸形调用。
                raise RuntimeError(f"未知的调用体语法形态: {kind!r}")
            m = llguidance.LLMatcher(self._ltok, grammar)
            if m.is_error():
                raise RuntimeError(f"llguidance grammar error: {m.get_error()}")
            self._matchers[key] = m
        return m


class ToolCallGuard:
    """单次生成的约束状态机（纯逻辑，不做数组运算）。

    IDLE：不约束；消费到 stc 标记 → ACTIVE。
    ACTIVE：llguidance bitmask 决策；调用体到达可接受态 → 只放行 etc → IDLE。
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
            # 调用体已完整：只放行收尾标记，强制立刻闭合（不给模型画蛇添足的机会）
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
