"""流式复读护栏：生成途中识别「模型陷进复读」并当场打断。

病根：`repetition_penalty`（engine 采样参数）只是采样偏置，压不住长程循环——实测会出现
整屏 `123.45 123.45 …`、同一行「营收 1,234 万元」刷几十遍、思考块里同一句话转圈，
一直烧到 max_tokens 才停（再被截断恢复升档放大）。本模块是纯逻辑检测器：引擎把每段
增量按频道喂进来，命中即返回 RepetitionHit，由引擎用取消同一条路停掉生成。

两条判据，任一命中即报（每个频道各一份滚动缓冲，频道之间互不串）：
  (a) n-gram 重复率：该频道累计 ≥ min_chars 后，最近 window_chars 个字符里的字符
      n-gram 中「与窗口内更早某个 n-gram 相同」的占比 ≥ max_dup_ratio。
      占比 = (总数 − 去重后个数) / 总数，与逐个比对「之前出现过」等价。
  (b) 连续同行：同一非空行（strip 后 ≥ 8 字符且含 ≥ 4 个字母/汉字）连续出现 ≥ line_repeats 次
      （空行不打断连续，其余任何不同的行都打断）。只认带文字的行：全零矩阵 `[0, 0, …],`、
      空模板表格 `|      |      |` 这类合法重复行不在判据内（一审实测会误杀）；判据 (a) 仍兜底
      ——真正的整屏复读过了 min_chars 还是会被 n-gram 占比拦住。

判据 (a) 每攒够 _CHECK_STRIDE 个新字符才算一次（推理线程热路径：逐 token 全窗口重算
会在长生成上累计出秒级 CPU），命中延迟至多一个步长。
阈值来自 config.yaml `guardrails.repetition`，缺键取本模块缺省；`enabled: false` → 不建检测器。
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional

CHANNELS = ("thinking", "content")
_CHECK_STRIDE = 64          # 判据 (a) 的重算步长（字符）
_MIN_LINE_CHARS = 8         # 判据 (b) 只认 strip 后至少这么长的行
_MIN_LINE_ALPHA = 4         # 判据 (b) 只认含这么多字母/汉字的行（纯数字/标点行不算）
_SAMPLE_CHARS = 80          # 命中样本上限（只进日志，不进事件流；日志里会带模型原文，故压短）

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "window_chars": 2000,
    "min_chars": 600,
    "ngram": 16,
    "max_dup_ratio": 0.80,
    "line_repeats": 10,
}


@dataclass(frozen=True)
class RepetitionHit:
    channel: str            # "thinking" | "content"
    reason: str             # "ngram_dup" | "line_repeat"
    sample: str             # ≤ 120 字符，仅供日志


class _ChannelState:
    __slots__ = ("window", "total", "since_check", "partial", "last_line", "line_count")

    def __init__(self) -> None:
        self.window = ""        # 最近 window_chars 个字符
        self.total = 0          # 本频道累计字符数
        self.since_check = 0    # 距上次判据 (a) 重算新增的字符数
        self.partial = ""       # 尚未收尾的当前行
        self.last_line: Optional[str] = None
        self.line_count = 0


class RepetitionDetector:
    def __init__(self, window_chars: int = 2000, min_chars: int = 600, ngram: int = 16,
                 max_dup_ratio: float = 0.80, line_repeats: int = 10):
        self.window_chars = max(1, int(window_chars))
        self.min_chars = max(0, int(min_chars))
        self.ngram = max(1, int(ngram))
        self.max_dup_ratio = float(max_dup_ratio)
        self.line_repeats = max(2, int(line_repeats))
        self._state: Dict[str, _ChannelState] = {}

    @classmethod
    def from_config(cls, cfg: Optional[Dict[str, Any]]) -> Optional["RepetitionDetector"]:
        """按 `guardrails.repetition` 段建检测器；关闭返回 None。坏值逐键回落缺省并告警。"""
        cfg = cfg if isinstance(cfg, dict) else {}
        enabled = cfg.get("enabled", DEFAULTS["enabled"])
        if enabled is False or enabled == 0 or \
                (isinstance(enabled, str) and enabled.strip().lower() in ("false", "0", "no", "off")):
            return None
        kwargs: Dict[str, Any] = {}
        for key, conv in (("window_chars", int), ("min_chars", int), ("ngram", int),
                          ("max_dup_ratio", float), ("line_repeats", int)):
            raw = cfg.get(key, DEFAULTS[key])
            try:
                val = conv(raw)
                # 占比必须落在 (0, 1]：0/负数会让一切命中，NaN/>1 会让一切放过
                if key == "max_dup_ratio" and not (0.0 < val <= 1.0):
                    raise ValueError(val)
            except (TypeError, ValueError):
                print(f"[Guard] guardrails.repetition.{key}={raw!r} 非法，取缺省 {DEFAULTS[key]}")
                val = DEFAULTS[key]
            kwargs[key] = val
        return cls(**kwargs)

    def feed(self, channel: str, text: str) -> Optional[RepetitionHit]:
        if not text:
            return None
        st = self._state.get(channel)
        if st is None:
            st = self._state[channel] = _ChannelState()
        st.window = (st.window + text)[-self.window_chars:]
        st.total += len(text)
        st.since_check += len(text)

        hit = self._feed_lines(channel, st, text)
        if hit is not None:
            return hit
        if st.total >= self.min_chars and st.since_check >= _CHECK_STRIDE:
            st.since_check = 0
            ratio = self._dup_ratio(st.window)
            if ratio >= self.max_dup_ratio:
                return RepetitionHit(channel, "ngram_dup", st.window[-_SAMPLE_CHARS:])
        return None

    def _dup_ratio(self, w: str) -> float:
        n = self.ngram
        total = len(w) - n + 1
        if total <= 0:
            return 0.0
        unique = len({w[i:i + n] for i in range(total)})
        return (total - unique) / total

    def _feed_lines(self, channel: str, st: _ChannelState, text: str) -> Optional[RepetitionHit]:
        buf = st.partial + text
        *done, st.partial = buf.split("\n")
        for line in done:
            s = line.strip()
            if not s:
                continue                      # 空行不打断连续
            if s == st.last_line:
                st.line_count += 1
            else:
                st.last_line, st.line_count = s, 1
            if (len(s) >= _MIN_LINE_CHARS and st.line_count >= self.line_repeats
                    and sum(ch.isalpha() for ch in s) >= _MIN_LINE_ALPHA):
                return RepetitionHit(channel, "line_repeat", s[:_SAMPLE_CHARS])
        if len(st.partial) > self.window_chars:   # 无换行的长串：行判据无从谈起，别无界攒
            st.partial = st.partial[-self.window_chars:]
        return None
