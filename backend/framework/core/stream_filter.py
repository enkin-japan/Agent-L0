"""
增量可见正文过滤器（真流式 P1 的核心 + Phase 3 的粒度钩子）。

把模型的原始 token 流（含频道标记与工具调用区块）**增量**过滤成"用户可见正文"，
语义与 ResponseParser.parse 的最终 text 对齐——两者对同一段完整输出应给出一致结果：

- thought 频道 `<|channel>thought …<channel|>` 内容：不进正文;经 on_thinking 钩子
  **逐段回吐**（Phase 3：思考直播,客户端契约为 append-only 增量,不依赖块边界）；
- 文本频道 `<|channel>text CONTENT<channel|>`：只吐 CONTENT（吃掉频道名 "text " 前缀）；
- 工具调用区块 `<|tool_call>…<tool_call|>`：不进正文;块内一旦解析出工具名即经
  on_tool_pending 钩子预告一次（Phase 3：执行前几十秒就知道要调什么,与约束采样
  开关无关——本过滤器看得到全部原始流）；
- 频道外的裸文本：照吐（模型常不加频道包裹直接给最终答复）；
- 标记 token 本身永不吐。

跨 delta 切分的标记安全处理：缓冲尾部保留 (maxlen-1) 个可能构成标记前缀的字符不吐，
待更多输入或 close() 再定夺，杜绝把半个 `<|channel` 误当正文送出。

注意：引擎在 enable_thinking 时把 prompt 收尾在一个已打开的 thought 频道里（见 engine
的 generated_text 播种），模型续写即处于该频道内——故此时 streamer 须以 in_thought=True
的抑制态起步，否则会把思考内容误当正文流出。

钩子在推理线程被同步调用（engine 负责用 call_soon_threadsafe 回主循环），
本层不做线程跳转。
"""
import re
from typing import Callable, List, Optional, Tuple

# 工具名预告的解析:块首若干字符内找 call: name { 前缀(gemma-4 紧凑 DSL,§7-8 起
# 是该方言唯一形态)、{"name": "..."}(纯 JSON 约束产物与迁移前历史)、或
# <function=name>(qwen35 方言的 XML 调用体)。三种形态都要求名字之后出现边界符
# (闭引号 / 左花括号 / '>')才算完整——逐 token 喂入时名字可能只生成了一半,
# 过早匹配会预告出残缺工具名。字符集含 '.'(Gemini 函数名规范,response_parser 同口径)。
_TOOL_NAME_RE = re.compile(
    r'"name"\s*:\s*"([A-Za-z0-9_.-]+)"'
    r'|call\s*:\s*"?([A-Za-z0-9_.-]+)"?\s*(?=\{)'
    r'|<function=([A-Za-z0-9_.-]+)>')
_TOOL_NAME_SCAN_LIMIT = 200  # 超过此长度还没出现 name 就放弃预告(不影响执行)


class VisibleTextStreamer:
    # 状态
    OUT, NAME, TEXTCH, OTHERCH, TOOL = range(5)

    def __init__(self, soc: str = "<|channel>", eoc: str = "<channel|>",
                 stc: str = "<|tool_call>", etc: str = "<tool_call|>",
                 in_thought: bool = False,
                 on_thinking: Optional[Callable[[str], None]] = None,
                 on_tool_pending: Optional[Callable[[str], None]] = None):
        self.soc, self.eoc, self.stc, self.etc = soc, eoc, stc, etc
        self._maxlen = max(len(soc), len(eoc), len(stc), len(etc))
        self.buf = ""
        self.mode = self.OTHERCH if in_thought else self.OUT
        self._name = "thought" if in_thought else ""  # NAME 态下累积的频道名(播种态即思考)
        self.on_thinking = on_thinking
        self.on_tool_pending = on_tool_pending
        self._tool_buf = ""        # 当前工具块已累积文本(仅为解析 name,有限长)
        self._tool_announced = False

    # ---- 标记匹配：在 buf[i:] 处尝试匹配给定标记集合 ----
    def _match(self, i: int, markers: List[str], final: bool) -> Tuple[str, str]:
        """
        返回 ("hit", marker) 命中完整标记；("hold", "") 可能是跨块的部分标记需等待；
        ("none", "") 此处不是任何标记起始。
        """
        rest = self.buf[i:]
        for m in markers:
            if rest.startswith(m):
                return ("hit", m)
        if not final:
            for m in markers:
                # rest 是某标记的真前缀（更短且相等）→ 需要更多输入
                if len(rest) < len(m) and m.startswith(rest):
                    return ("hold", "")
        return ("none", "")

    def feed(self, delta: str, final: bool = False) -> str:
        self.buf += delta
        out: List[str] = []
        thinking: List[str] = []   # 本次 feed 产出的思考增量(批量回吐一次)
        i = 0
        n = len(self.buf)
        while i < n:
            if self.mode == self.OUT:
                # eoc 也在 OUT 态匹配：思考预算强制收尾后模型可能补发无配对的 <channel|>，
                # 属标记而非正文，吞掉（与 ResponseParser 第 3 步的游离 eoc 清除对齐）。
                st, m = self._match(i, [self.soc, self.stc, self.eoc], final)
                if st == "hold":
                    break
                if st == "hit":
                    if m == self.soc:
                        self.mode = self.NAME
                        self._name = ""
                    elif m == self.stc:
                        self.mode = self.TOOL
                        self._tool_buf = ""
                        self._tool_announced = False
                    # m == eoc：游离收尾标记，吞掉、保持 OUT
                    i += len(m)
                    continue
                # 频道外裸文本：照吐（但吃掉孤立的 <|…|> 标记残片由 _match 的 hold/hit 覆盖）
                out.append(self.buf[i])
                i += 1

            elif self.mode == self.NAME:
                ch = self.buf[i]
                # eoc 可能紧跟（空内容频道）
                st, m = self._match(i, [self.eoc], final)
                if st == "hold":
                    break
                if st == "hit":
                    self.mode = self.OUT
                    i += len(m)
                    continue
                if ch.isspace():
                    # 频道名结束：决定去向，吃掉这个分隔空白
                    self.mode = self.TEXTCH if self._name == "text" else self.OTHERCH
                    i += 1
                    continue
                self._name += ch
                i += 1

            elif self.mode == self.TEXTCH:
                st, m = self._match(i, [self.eoc], final)
                if st == "hold":
                    break
                if st == "hit":
                    self.mode = self.OUT
                    i += len(m)
                    continue
                out.append(self.buf[i])
                i += 1

            elif self.mode == self.OTHERCH:
                st, m = self._match(i, [self.eoc], final)
                if st == "hold":
                    break
                if st == "hit":
                    self.mode = self.OUT
                    i += len(m)
                    continue
                # thought 频道内容:不进正文,但作为思考增量直播(其余未知频道仍纯抑制)
                if self._name == "thought":
                    thinking.append(self.buf[i])
                i += 1

            else:  # TOOL
                st, m = self._match(i, [self.etc], final)
                if st == "hold":
                    break
                if st == "hit":
                    self.mode = self.OUT
                    i += len(m)
                    continue
                # 工具块内容:累积有限长文本,一解析出工具名即预告一次
                if not self._tool_announced and len(self._tool_buf) < _TOOL_NAME_SCAN_LIMIT:
                    self._tool_buf += self.buf[i]
                    if (m2 := _TOOL_NAME_RE.search(self._tool_buf)) is not None:
                        self._tool_announced = True
                        if self.on_tool_pending is not None:
                            self.on_tool_pending(m2.group(1) or m2.group(2) or m2.group(3))
                i += 1

        # 未处理的尾部（holdback 或未闭合标记）留待下次
        self.buf = self.buf[i:]
        if thinking and self.on_thinking is not None:
            self.on_thinking("".join(thinking))
        return "".join(out)

    def close(self) -> str:
        """流结束：把残留缓冲按 final 语义再跑一遍（不再 holdback），吐出安全的尾部正文。"""
        tail = self.feed("", final=True)
        # close 后 buf 里若仍有内容，说明是未闭合频道/工具块的残留，按当前 mode 语义丢弃
        self.buf = ""
        return tail
