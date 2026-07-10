"""
增量可见正文过滤器（真流式 P1 的核心）。

把模型的原始 token 流（含频道标记与工具调用区块）**增量**过滤成"用户可见正文"，
语义与 ResponseParser.parse 的最终 text 对齐——两者对同一段完整输出应给出一致结果：

- thought 频道 `<|channel>thought …<channel|>` 内容：抑制（思考按决策 #5 整块另发，不逐字）；
- 文本频道 `<|channel>text CONTENT<channel|>`：只吐 CONTENT（吃掉频道名 "text " 前缀）；
- 工具调用区块 `<|tool_call>…<tool_call|>`：抑制（结构化，另作 tool_started 事件）；
- 频道外的裸文本：照吐（模型常不加频道包裹直接给最终答复）；
- 标记 token 本身永不吐。

跨 delta 切分的标记安全处理：缓冲尾部保留 (maxlen-1) 个可能构成标记前缀的字符不吐，
待更多输入或 close() 再定夺，杜绝把半个 `<|channel` 误当正文送出。

注意：引擎在 enable_thinking 时把 prompt 收尾在一个已打开的 thought 频道里（见 engine
的 generated_text 播种），模型续写即处于该频道内——故此时 streamer 须以 in_thought=True
的抑制态起步，否则会把思考内容误当正文流出。
"""
from typing import List, Tuple


class VisibleTextStreamer:
    # 状态
    OUT, NAME, TEXTCH, OTHERCH, TOOL = range(5)

    def __init__(self, soc: str = "<|channel>", eoc: str = "<channel|>",
                 stc: str = "<|tool_call>", etc: str = "<tool_call|>",
                 in_thought: bool = False):
        self.soc, self.eoc, self.stc, self.etc = soc, eoc, stc, etc
        self._maxlen = max(len(soc), len(eoc), len(stc), len(etc))
        self.buf = ""
        self.mode = self.OTHERCH if in_thought else self.OUT
        self._name = ""  # NAME 态下累积的频道名

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
                i += 1  # 抑制

            else:  # TOOL
                st, m = self._match(i, [self.etc], final)
                if st == "hold":
                    break
                if st == "hit":
                    self.mode = self.OUT
                    i += len(m)
                    continue
                i += 1  # 抑制

        # 未处理的尾部（holdback 或未闭合标记）留待下次
        self.buf = self.buf[i:]
        return "".join(out)

    def close(self) -> str:
        """流结束：把残留缓冲按 final 语义再跑一遍（不再 holdback），吐出安全的尾部正文。"""
        tail = self.feed("", final=True)
        # close 后 buf 里若仍有内容，说明是未闭合频道/工具块的残留，按当前 mode 语义丢弃
        self.buf = ""
        return tail
