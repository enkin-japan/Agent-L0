"""确定性「真实形态」历史填充 + 精确回忆事实卡（context_knee W0）。

形态（与编排层落进 session.history 的同构，压缩层看得见）：每个伪轮次 =
  user 短问
  → assistant {thoughts 1–3 句, content "", tool_calls [一条调用]}
  → tool {"tool_responses": [{"name", "response": <json 串>, "_failed", ["_safe", "_target"]}]}
  → assistant 收尾正文
观察体用 model_eval mock 环境的真实回执形态（list_files / read_file / todo_list /
write_file 的 JSON，与 mock_envs.env_response_content 同一序列化），内容是合成的
运维文件（日志块 / YAML 配置 / runbook / CSV / 指标 JSON），不含任何个人数据。
`response` 键（不是 executor 回喂用的 `content`）是 orchestrator.py:1844 的形态——
compactor 只量、只改 `response`；`_safe/_target` 照 orchestrator.py:1854-1858 挂在
只读调用上（方言渲染只取 name+观测体，这两个键不进 prompt）。

确定性与超集：同一 (seed, rep, task_key, 工具集) 的伪轮次流固定（task_key = 题面指纹，
填充按题独立）；某档取流的前 n 轮，n 随目标 token 单调——同一题内高档填充的伪轮次是
低档的超集（只增不换）。事实卡**按档重新落位**（各档的 10% / 50% / 90% 处），卡种在
三个槽位间按 rotation 轮换；卡值同一 rep 内跨档不变（除非查重重抽，见 build_filler）。

与 mock 环境的隔离：伪历史的全部路径落在 LEGACY_ROOT 命名空间（与 longchain 全部题面
的 env_init 路径/目录不相交，测试钉住），每条伪 user 消息都点明「上一个已归档、与当前
无关的项目」。残余：模型仍可能回读伪文件 → mock 环境报 File not found → env_errors。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

# ---- 事实卡 ----

#: 三张卡的落位（填充 token 轴上的目标分位）。键名即 recall_by_slot 的键。
CARD_SLOTS: Tuple[Tuple[str, float], ...] = (("front", 0.10), ("mid", 0.50), ("back", 0.90))
#: 卡种的基准顺序；槽位 i 分到 CARD_KINDS[(i + rotation) % 3]（rotation = 题序 + rep），
#: 让位置效应与卡种难度可分。
CARD_KINDS: Tuple[str, ...] = ("config_path", "ticket", "threshold")
#: 伪历史的命名空间（绝对路径；题面全是相对路径，且目录名互不相交）
LEGACY_ROOT = "/srv/legacy-2025q3"
_LEGACY_NOTE = {"zh": "（这是上一个已归档、与当前无关的项目 legacy-2025q3）",
                "en": "(This is the previous, archived project legacy-2025q3, unrelated to current work.) "}
MAX_CARD_REDRAWS = 20
MAX_PSEUDO_TURNS = 5000

# 填充正文里绝不出现这些串（卡的唯一性靠它；_assert_clean 在生成期守门）
_CARD_MARKERS = ("halyard", "OPS-", "queue_depth_alert_threshold")

RECALL_SUFFIX = {
    "zh": ("\n\n另外：最终答复里必须逐字写出此前工具输出中出现过的这三项的值——"
           "① halyard 服务加载的配置文件路径；② 升级处理的工单号（OPS- 开头）；"
           "③ queue_depth_alert_threshold 的数值。"),
    "en": ("\n\nAlso: your final reply must state, verbatim, the values of these three items "
           "that appeared earlier in tool outputs — (1) the config file path the halyard "
           "service loaded; (2) the escalation ticket ID (starts with OPS-); (3) the value of "
           "queue_depth_alert_threshold."),
    "ja": ("\n\nなお、最終回答では、以前のツール出力に出てきた次の三項目の値をそのまま"
           "書き写してください——① halyard サービスが読み込んだ設定ファイルのパス；"
           "② エスカレーションのチケット番号（OPS- で始まる）；③ queue_depth_alert_threshold の値。"),
}


def recall_suffix(lang: str) -> str:
    return RECALL_SUFFIX.get(lang, RECALL_SUFFIX["en"])


def card_values(seed: int, rep: int, attempt: int = 0) -> Dict[str, str]:
    """卡值：只依赖 (seed, rep, attempt)——同一 rep 跨档、跨题不变，rep 之间不同；
    attempt>0 是查重撞车后的确定性重抽。"""
    rng = random.Random(f"knee-cards:{seed}:{rep}" + (f":{attempt}" if attempt else ""))
    hex6 = "".join(rng.choice("0123456789abcdef") for _ in range(6))
    return {"config_path": f"/opt/halyard/conf/halyard-{hex6}.toml",
            "ticket": f"OPS-{rng.randint(10000, 99999)}",
            "threshold": str(rng.randint(1200, 9800))}


def card_line(kind: str, value: str, host_kind: str, ts: str = "") -> str:
    body = {"config_path": f"halyard: loaded config from {value}",
            "ticket": f"escalation: incident tracked as ticket {value}",
            "threshold": f"queue_depth_alert_threshold = {value}"}[kind]
    if host_kind == "log":
        return f"{ts} INFO  [ops-audit] {body}"
    if host_kind == "md":
        return f"- {body}"
    return body


_DASHES = dict.fromkeys(range(0x2010, 0x2016), "-")


def normalize_answer(text: str) -> str:
    """NFKC（全角数字/字母 → 半角）+ U+2010–U+2015 各式短横一律换成 '-'。"""
    return unicodedata.normalize("NFKC", text or "").translate(_DASHES)


def value_pattern(value: str) -> "re.Pattern[str]":
    """值作为整体出现：两侧不紧贴字母数字（7340 不算命中 17340），且后面不紧跟
    '.' 加数字（7340.5 不算命中 7340）。查重与回忆评分共用这一条。"""
    v = re.escape(str(value))
    return re.compile(rf"(?<![0-9A-Za-z]){v}(?![0-9A-Za-z])(?!\.[0-9])")


def score_recall(final_text: str, cards: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    text = normalize_answer(final_text)
    by_slot: Dict[str, bool] = {}
    by_kind: Dict[str, bool] = {}
    for c in cards:
        hit = value_pattern(c["value"]).search(text) is not None
        by_slot[c["slot"]] = hit
        if c.get("kind"):
            by_kind[c["kind"]] = hit
    hits = sum(by_slot.values())
    return {"recall_ok": bool(cards) and hits == len(cards), "recall_hits": hits,
            "recall_by_slot": by_slot, "recall_by_kind": by_kind}


# ---- 合成内容 ----

_SERVICES = ["ingest", "relay", "ledger", "scheduler", "gateway", "indexer", "billing-sync",
             "thumbnailer", "mailer", "search-api", "cache-warm", "report-gen", "feed-merge",
             "geo-tiles", "audit-trail", "quota-svc"]
_LEVELS = ["INFO ", "INFO ", "INFO ", "DEBUG", "WARN ", "ERROR"]
_LOG_MSGS = [
    "batch {n} committed rows={m} lag_ms={k}",
    "worker heartbeat ok queue={m} inflight={k}",
    "retrying upstream call attempt={k} backoff_ms={m}",
    "slow query {k}ms table=events_{n} plan=index_scan",
    "cache miss ratio {r} over last {k}s",
    "rotated segment seg-{n}.dat size_mb={k}",
    "connection pool resized from {k} to {m}",
    "schema check passed version={n}",
    "dropped {k} malformed records from partition {n}",
    "gc pause {k}ms heap_mb={m}",
]
_YAML_KEYS = ["replicas", "timeout_ms", "max_batch", "retry_limit", "log_level", "pool_size",
              "flush_interval_s", "region", "shard_count", "read_only", "warmup_s", "cpu_limit"]
_MD_LINES = [
    "## 巡检步骤", "## Rollback", "## 已知问题", "## Contacts (role-based)",
    "- 先确认上游队列是否积压，再看 worker 心跳", "- Check the dashboard panel `lag_p95` before restarting",
    "- 重启顺序：gateway → relay → ingest", "- 回滚只回到上一个 tag，不跨版本",
    "- If disk usage > 85%, rotate segments first", "- 夜间窗口 02:00–04:00 禁止变更",
    "- 发布后观察 15 分钟错误率", "- Escalate to the on-call rotation if two retries fail",
    "- 配置变更必须走评审", "- Do not purge the dead-letter queue without a snapshot",
]
_USER_Q = {
    "file": ["看一下 {path} 里有没有异常", "{path} 最近都记了些什么？", "帮我读一下 {path}",
             "Check {path} for anything unusual", "{svc} 那份 {path} 里写了什么？"],
    "listing": ["列一下现在都有哪些文件", "目录里有什么？", "List the files again please"],
    "write": ["把刚才 {svc} 的巡检结论记到 {path}", "Write a short note about {svc} to {path}"],
    "error": ["打开 {path} 看看", "Read {path}"],
    "todos": ["现在待办清单是什么状态？", "Show me the todo list", "还有哪些没做完？"],
    "todo_add": ["加一条待办：{text}", "Add a todo: {text}"],
}
_THOUGHTS = {
    "file": ["The user wants the contents of {path}, so I should read it before answering.",
             "I need to look at the file first; guessing would be unreliable.",
             "Let me fetch {path} and then summarize the notable lines.",
             "After reading I will report counts rather than paste the whole file."],
    "listing": ["A listing will tell me which files exist before I open anything.",
                "I should not assume the directory layout; list it first."],
    "write": ["I'll write the note now; the content is already decided from the earlier read.",
              "The note goes to {path}; one write call is enough."],
    "error": ["The user named {path}; I'll try to read it directly.",
              "Let me open {path} as asked."],
    "todos": ["Checking the todo list is the quickest way to answer this.",
              "I need the current todo state before saying anything."],
    "todo_add": ["Adding the item is a single call.", "The text is given verbatim; add it as-is."],
}
_THOUGHT_TAIL = ["Only one tool call is needed here.", "No other tool is required for this step."]
_CLOSING = [
    "读完了：{path} 共 {lines} 行，其中 {warn} 条 WARN/ERROR，暂时没有需要立即处理的。",
    "Done. {path} has {lines} lines; {warn} of them are warnings or errors.",
    "已记录。{svc} 这边目前看不出明显问题，后续按 runbook 观察即可。",
    "旧项目目录里有 {files} 个文件，按服务分在 log、deploy、runbook 等几个子目录下。",
    "待办清单共 {lines} 项，其中 {warn} 项已完成。",
    "That file does not exist; the path was probably mistyped. Nothing else to do here.",
    "好的，已经加上了。",
]


@dataclass
class PseudoTurn:
    """一个伪轮次。lines 非空的（file / todos 类）可承载事实卡。"""
    idx: int
    kind: str                 # file | listing | write | error | todos | todo_add
    host_kind: str            # log | yaml | md | csv | json | todos | -
    user: str
    thoughts: str
    call: Dict[str, Any]
    path: str = ""
    lines: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)   # listing files / todo flags / ack
    closing: str = ""

    @property
    def card_eligible(self) -> bool:
        return self.host_kind in ("log", "yaml", "md", "todos") and len(self.lines) >= 2

    def observation(self, inserts: Sequence[Tuple[int, str]] = ()) -> str:
        """观察体（json 串，与 mock_envs.env_response_content 同一序列化）。
        inserts: [(行下标, 卡行)]——卡插在该行之前，原始行不变（只增不换）。"""
        flags = self.extra.get("done") or [False] * len(self.lines)
        rows = list(zip(self.lines, flags))
        for pos, text in sorted(inserts, key=lambda t: -t[0]):
            rows.insert(pos, (text, False))
        lines = [r[0] for r in rows]
        if self.kind == "file":
            return json.dumps({"path": self.path, "content": "\n".join(lines)}, ensure_ascii=False)
        if self.kind == "todos":
            return json.dumps({"todos": [{"text": t, "done": bool(d)} for t, d in rows]},
                              ensure_ascii=False)
        if self.kind == "listing":
            return json.dumps({"files": self.extra["files"]}, ensure_ascii=False)
        if self.kind == "write":
            return json.dumps({"status": "written", "path": self.path}, ensure_ascii=False)
        if self.kind == "todo_add":
            return json.dumps({"status": "added", "text": self.extra["text"]}, ensure_ascii=False)
        if self.kind == "error":
            return json.dumps({"error": f"File not found: {self.path}"}, ensure_ascii=False)
        raise ValueError(self.kind)

    def messages(self, inserts: Sequence[Tuple[int, str]] = ()) -> List[Dict[str, Any]]:
        name = self.call["name"]
        obs: Dict[str, Any] = {"name": name, "response": self.observation(inserts),
                               "_failed": self.kind == "error"}
        if name in ("list_files", "read_file", "todo_list"):
            obs["_safe"] = True
            if self.path:
                obs["_target"] = self.path
        return [
            {"role": "user", "content": self.user},
            {"role": "assistant", "thoughts": self.thoughts, "content": "",
             "tool_calls": [self.call]},
            {"role": "tool", "tool_responses": [obs]},
            {"role": "assistant", "thoughts": "", "content": self.closing, "tool_calls": []},
        ]


def _target_len(rng: random.Random) -> int:
    """观察体目标长度：300–3000 字，对数均匀（短观察多、长观察少，像真实历史）。"""
    return int(math.exp(rng.uniform(math.log(300), math.log(3000))))


def _ts(rng: random.Random, base_min: int) -> str:
    m = base_min + rng.randint(0, 59)
    return f"2026-09-{1 + (m // 1440) % 28:02d}T{(m // 60) % 24:02d}:{m % 60:02d}:{rng.randint(0, 59):02d}Z"


def _gen_lines(rng: random.Random, host_kind: str, svc: str, target: int,
               wrap: Callable[[List[str]], str]) -> List[str]:
    lines: List[str] = []
    base_min = rng.randint(0, 40000)
    if host_kind == "csv":
        lines.append("date,region,sku,qty,unit_price")
    if host_kind == "md":
        lines.append(f"# {svc} runbook")
    while len(wrap(lines)) < target:
        if host_kind == "log":
            msg = rng.choice(_LOG_MSGS).format(n=rng.randint(100, 9999), m=rng.randint(1, 5000),
                                               k=rng.randint(1, 900), r=round(rng.random(), 3))
            lines.append(f"{_ts(rng, base_min)} {rng.choice(_LEVELS)} [{svc}-{rng.randint(1, 8)}] {msg}")
            base_min += rng.randint(0, 3)
        elif host_kind == "yaml":
            k = rng.choice(_YAML_KEYS)
            v = rng.choice([str(rng.randint(1, 4000)), "true", "false", "ap-east-2", "warn", "info"])
            lines.append(f"{'  ' * rng.randint(0, 2)}{k}: {v}")
        elif host_kind == "md":
            lines.append(rng.choice(_MD_LINES))
        elif host_kind == "csv":
            lines.append(f"2026-0{rng.randint(1, 9)}-{rng.randint(10, 28)},r{rng.randint(1, 6)},"
                         f"SKU-{rng.randint(100, 999)},{rng.randint(1, 80)},{rng.randint(5, 400)}.{rng.randint(0, 99):02d}")
        elif host_kind == "json":
            lines.append(f'  "{rng.choice(["p50_ms", "p95_ms", "err_rate", "qps", "cpu_pct"])}_{len(lines)}": '
                         f'{round(rng.uniform(0, 900), 2)},')
        elif host_kind == "todos":
            lines.append(f"legacy-2025q3 {rng.choice(['巡检', 'Review', '清理', 'Rotate', '核对', 'Patch'])} "
                         f"{rng.choice(_SERVICES)} #{rng.randint(10, 999)}")
    if host_kind == "json":
        lines = ["{"] + lines + ['  "ok": true', "}"]
    return lines


def _legacy_path(rng: random.Random, host_kind: str, svc: str) -> str:
    return {"log": f"{LEGACY_ROOT}/{svc}/log/{svc}-{rng.randint(1, 28):02d}.log",
            "yaml": f"{LEGACY_ROOT}/{svc}/deploy/values.yaml",
            "md": f"{LEGACY_ROOT}/runbook/{svc}.md",
            "csv": f"{LEGACY_ROOT}/export/{svc}-{rng.randint(1, 40)}.csv",
            "json": f"{LEGACY_ROOT}/telemetry/{svc}-{rng.randint(1, 12)}.json"}[host_kind]


def _assert_clean(turn: PseudoTurn) -> None:
    blob = json.dumps(turn.messages(), ensure_ascii=False)
    for m in _CARD_MARKERS:
        if m in blob:
            raise AssertionError(f"填充正文混入事实卡标记 {m!r}（卡唯一性被破坏）")


def iter_pseudo_turns(seed: int, rep: int, tool_names: Sequence[str],
                      task_key: str = "") -> Iterator[PseudoTurn]:
    """(seed, rep, task_key, 工具集) 决定的无穷伪轮次流。工具名只取题面声明的那些——
    填充里不出现模型手里没有的工具。"""
    names = set(tool_names)
    has_files, has_todos = "read_file" in names, "todo_list" in names
    if not (has_files or has_todos):
        raise ValueError(f"填充需要 read_file 或 todo_list 工具，题面只有 {sorted(names)}")
    rng = random.Random(f"knee-filler:{seed}:{rep}:{task_key}:{','.join(sorted(names))}")
    weights: List[Tuple[str, float]] = []
    if has_files:
        weights += [("file", 0.62), ("listing", 0.08), ("write", 0.12 if "write_file" in names else 0),
                    ("error", 0.05)]
    if has_todos:
        weights += [("todos", 0.13 if has_files else 0.8),
                    ("todo_add", 0.05 if "todo_add" in names else 0)]
    kinds = [k for k, w in weights if w > 0]
    probs = [w for _, w in weights if w > 0]
    known_paths: List[str] = []
    i = 0
    while True:
        kind = rng.choices(kinds, probs)[0]
        svc = rng.choice(_SERVICES)
        host_kind = "-"
        path, lines, extra = "", [], {}
        if kind in ("file", "write", "error"):
            host_kind = rng.choices(["log", "yaml", "md", "csv", "json"], [0.4, 0.2, 0.15, 0.15, 0.1])[0]
            path = _legacy_path(rng, host_kind, svc)
        target = _target_len(rng)
        if kind == "file":
            lines = _gen_lines(rng, host_kind, svc, target,
                               lambda ls: json.dumps({"path": path, "content": "\n".join(ls)},
                                                     ensure_ascii=False))
            call = {"type": "function", "name": "read_file", "arguments": {"path": path}}
            known_paths.append(path)
        elif kind == "todos":
            host_kind = "todos"
            lines = _gen_lines(rng, "todos", svc, target,
                               lambda ls: json.dumps({"todos": [{"text": t, "done": False} for t in ls]},
                                                     ensure_ascii=False))
            extra["done"] = [rng.random() < 0.4 for _ in lines]
            call = {"type": "function", "name": "todo_list", "arguments": {}}
        elif kind == "listing":
            pool = sorted(set(known_paths)) or [_legacy_path(rng, "log", svc)]
            files = list(pool)
            while len(json.dumps({"files": files})) < target:
                files.append(_legacy_path(rng, rng.choice(["log", "yaml", "md", "csv"]),
                                          rng.choice(_SERVICES)))
            extra["files"] = sorted(set(files))
            call = {"type": "function", "name": "list_files", "arguments": {}}
        elif kind == "write":
            body = "\n".join(_gen_lines(rng, "md", svc, rng.randint(300, 1500), lambda ls: "\n".join(ls)))
            path = f"{LEGACY_ROOT}/handoff/{svc}-check-{rng.randint(1, 99)}.md"
            host_kind = "-"
            call = {"type": "function", "name": "write_file", "arguments": {"path": path, "content": body}}
        elif kind == "todo_add":
            extra["text"] = f"legacy-2025q3 跟进 {svc} #{rng.randint(10, 999)}"
            call = {"type": "function", "name": "todo_add", "arguments": {"text": extra["text"]}}
        else:  # error
            path = path.replace(".", "-old.", 1) if "." in path else path + "-old"
            host_kind = "-"
            call = {"type": "function", "name": "read_file", "arguments": {"path": path}}
        fmt = {"path": path or f"{svc}", "svc": svc, "text": extra.get("text", ""),
               "lines": len(lines), "warn": sum(1 for ln in lines if "WARN" in ln or "ERROR" in ln)
               or sum(extra.get("done") or []), "files": len(extra.get("files") or [])}
        q_kind = kind if kind in _USER_Q else "file"
        q = rng.choice(_USER_Q[q_kind]).format(**fmt)
        is_zh = any("\u4e00" <= ch <= "\u9fff" for ch in q)
        user = _LEGACY_NOTE["zh" if is_zh else "en"] + q
        pool = _THOUGHTS[kind]
        picked = rng.sample(pool, rng.randint(1, len(pool)))[:2]
        if rng.random() < 0.5:
            picked.append(rng.choice(_THOUGHT_TAIL))
        thoughts = " ".join(picked).format(**fmt)
        closing = {"file": _CLOSING[rng.randint(0, 1)], "write": _CLOSING[2], "listing": _CLOSING[3],
                   "todos": _CLOSING[4], "error": _CLOSING[5], "todo_add": _CLOSING[6]}[kind].format(**fmt)
        turn = PseudoTurn(idx=i, kind=kind, host_kind=host_kind, user=user, thoughts=thoughts,
                          call=call, path=path, lines=lines, extra=extra, closing=closing)
        _assert_clean(turn)
        yield turn
        i += 1


# ---- 按目标装配 ----

@dataclass
class Filler:
    target_tokens: int
    turns: List[PseudoTurn]
    turn_costs: List[int]
    cards: List[Dict[str, Any]]                 # [{slot, kind, value, turn, line, frac_est}]
    messages: List[Dict[str, Any]]

    @property
    def est_tokens(self) -> int:
        return sum(self.turn_costs)

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.messages, ensure_ascii=False, sort_keys=True)
                              .encode()).hexdigest()[:16]


def build_filler(target_tokens: int, *, seed: int, rep: int, tool_names: Sequence[str],
                 turn_cost: Callable[[List[Dict[str, Any]]], int], task_key: str = "",
                 rotation: int = 0, min_turns: int = 3, with_cards: bool = True) -> Filler:
    """取伪轮次流前 n 轮，使估算 token 和最接近 target（target>0 时至少 min_turns 轮）。

    turn_cost(该轮 messages) → 该轮在 prompt 里的 token 增量（调用方用引擎分词器量）。
    n 只由各轮代价的前缀和决定 → 对 target 单调 → 高档是低档的超集。
    """
    if target_tokens <= 0:
        return Filler(0, [], [], [], [])
    turns: List[PseudoTurn] = []
    costs: List[int] = []
    total = 0
    for t in iter_pseudo_turns(seed, rep, tool_names, task_key):
        c = turn_cost(t.messages())
        if c <= 0 or len(turns) >= MAX_PSEUDO_TURNS:
            # 计数器失灵（代价恒 0 的分词器替身等）会让流无穷取下去——响亮失败而不是挂死
            raise ValueError(f"伪轮次代价 {c}、已取 {len(turns)} 轮仍未达目标 {target_tokens}："
                             f"计数器不可用")
        if len(turns) >= min_turns and total + c > target_tokens:
            # 超出：加上这一轮更接近目标才加（tie 不加）
            if (total + c - target_tokens) < (target_tokens - total):
                turns.append(t)
                costs.append(c)
                total += c
            break
        turns.append(t)
        costs.append(c)
        total += c
        if total >= target_tokens and len(turns) >= min_turns:
            break
    if not with_cards:
        messages = [m for t in turns for m in t.messages()]
        return Filler(target_tokens, turns, costs, [], messages)
    starts = [sum(costs[:i]) for i in range(len(costs))]
    eligible = [i for i, t in enumerate(turns) if t.card_eligible]
    if not eligible:
        raise ValueError("填充里没有可承载事实卡的观察（file/todos 类）——目标过小或工具集不支持")
    # 落位只依赖填充结构，与卡值无关；卡值撞车（值在填充别处也出现）就重抽
    placements = []
    for i_slot, (slot, frac) in enumerate(CARD_SLOTS):
        p = frac * total
        inside = [i for i in eligible if starts[i] <= p < starts[i] + costs[i]]
        ti = inside[0] if inside else min(
            eligible, key=lambda i: min(abs(p - starts[i]), abs(p - starts[i] - costs[i])))
        t = turns[ti]
        rel = min(max((p - starts[ti]) / max(costs[ti], 1), 0.0), 1.0)
        # 第 0 行是标题/表头时不插在它前面
        line = min(max(1, round(rel * len(t.lines))), len(t.lines))
        kind = CARD_KINDS[(i_slot + rotation) % len(CARD_KINDS)]
        frac_est = (starts[ti] + costs[ti] * line / max(len(t.lines), 1)) / max(total, 1)
        placements.append((slot, kind, ti, line, round(frac_est, 3)))
    for attempt in range(MAX_CARD_REDRAWS + 1):
        values = card_values(seed, rep, attempt)
        inserts: Dict[int, List[Tuple[int, str]]] = {}
        cards: List[Dict[str, Any]] = []
        for slot, kind, ti, line, frac_est in placements:
            ts_rng = random.Random(f"knee-card-ts:{seed}:{rep}:{slot}")
            text = card_line(kind, values[kind], turns[ti].host_kind, _ts(ts_rng, 20000))
            inserts.setdefault(ti, []).append((line, text))
            cards.append({"slot": slot, "kind": kind, "value": values[kind], "turn": ti,
                          "line": line, "frac_est": frac_est, "redraws": attempt})
        messages = []
        for i, t in enumerate(turns):
            messages.extend(t.messages(inserts.get(i, ())))
        blob = json.dumps(messages, ensure_ascii=False)
        if all(len(value_pattern(c["value"]).findall(blob)) == 1 for c in cards):
            break
    else:
        raise ValueError(f"卡值重抽 {MAX_CARD_REDRAWS} 次仍与填充正文撞车（seed={seed} rep={rep}）")
    return Filler(target_tokens, turns, costs, cards, messages)
