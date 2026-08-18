"""
上下文压缩（Step 1：机械式工具观察压缩）。

理念（见记忆管理讨论）：理想 Agent 以 task 为主、conversation 为辅，任务完成后其冗长的
原始工具观察对后续毫无复用价值，只会撑大上下文、拖慢 decode（Apple Silicon 上 decode
受内存带宽约束，KV 越大每 token 越慢）、并稀释模型注意力。故任务沉降后即把这些观察削成摘要。

本步只做「机械压缩」（无模型调用、确定性、幂等）。升级自纯 recency（T3-a/b，借鉴
Claude Code 的 microcompaction）——从「保最近 N 条、更早一律砍」改为「预算感知 + 取代感知」：
- 只削「工具观察」(role=tool 的 tool_responses 内容)——上下文里体量最大、最不需复用的部分；
  工具调用名、计划 note、最终答复、用户消息一律保留（对话骨架不动）。
- 取代去重（T3-b）：同一只读工具(_safe)对同一目标(_target，如同一文件/同一窗口/同一 query)
  的重复读取，较旧那条被后续读取「取代」削成占位桩；但两次读取之间只要出现过一次写/有副作用
  观察（屏障）就切断，绝不跨写取代——GUI「读树→点击→再读树」的两次树都完整保留。
- 预算驱逐（T3-a）：全部观察总字符超 budget_chars 时，在可压缩区（最近 keep_recent 条之外）
  按「体量大者优先（同体量取更旧）」压成首部摘要，直到回到预算内；小观察（≤ digest_chars）
  即使旧也留、大观察即使较新也先压。未越预算则旧观察保持原文（比旧策略更少误伤、更高保真）。
- 最近 keep_recent 条为硬保护窗口，永不改动（保任务内 KV 前缀复用与续跑连续性）；由此每次落刀
  都只动旧前缀区，KV 代价与旧策略一致。
- 只作用于内存中的 model-facing history；持久化日志（history.jsonl）保留全量，
  前端展示与审计不受影响。冷恢复后首次推理仍会经 run_turn 顶部再压一次，恢复上下文同样有界。
- 幂等：压过的观察打 _compacted 标记（仅存内存，永不落盘），重复调用不再处理。
  取代/预算所依赖的 _safe/_target 元数据由编排层捕获时挂上，同样仅内存、落盘前被剥离。

摘要式二次压缩（Step 2，方案A）：对已沉降的旧轮次做模型摘要，折叠进 memory.json 旁挂的
滚动摘要并推进「已摘要行数」水位线；全量日志保留，model-facing 上下文重建为
[system] + [摘要块] + [尾部轮次]。触发有两处（共用本函数）：
- 活跃高阈值：轮次落盘后若上下文越过高阈值即折叠（routes）；
- 空闲低阈值：会话长时间空闲将 freeze 前若越过低阈值即折叠（app 的空闲维护回合）。
"""
import asyncio
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import rag_sink
from .notes import NOTE_TYPES

# 模块级默认（与 config.yaml agent 段默认保持一致；编排层按 config 覆盖，其余调用方用默认）
DEFAULT_KEEP_RECENT = 6
DEFAULT_DIGEST_CHARS = 200
DEFAULT_BUDGET_CHARS = 20000  # 机械层观察总预算（保守偏大：越过才驱逐旧大观察，不越则保留原文）


def compact_tool_observations(history: List[Dict[str, Any]], *,
                              keep_recent: int = DEFAULT_KEEP_RECENT,
                              digest_chars: int = DEFAULT_DIGEST_CHARS,
                              budget_chars: Optional[int] = None) -> int:
    """
    机械压缩：取代去重（T3-b）+ 预算驱逐（T3-a）。原地修改 history，返回本次新压缩的观察条数。
    确定性、幂等、无模型调用。最近 keep_recent 条为硬保护窗口（永不改动）。
    budget_chars 为 None/0 时跳过预算阶段（仅做取代去重）。
    """
    # 按出现顺序收集所有工具观察项
    observations: List[Dict[str, Any]] = []
    for msg in history:
        if msg.get("role") == "tool":
            for tr in msg.get("tool_responses", []) or []:
                if isinstance(tr, dict):
                    observations.append(tr)

    n = len(observations)
    cutoff = n - keep_recent if keep_recent > 0 else n  # 索引 < cutoff 可压缩；>= cutoff 受保护
    compacted = 0

    # --- 阶段一：取代去重（T3-b） ---
    # 顺序扫描，按 epoch 划分「无夹写」的读取区间：同一 (name, target) 在同一 epoch 内重复出现，
    # 较旧那条被取代。遇到写/有副作用观察（_safe 非真）即推进 epoch，形成屏障，不跨屏障取代。
    epoch = 0
    last_seen: Dict[tuple, tuple] = {}  # (name, target) -> (obs_index, epoch)
    for i, tr in enumerate(observations):
        safe = tr.get("_safe") is True
        target = tr.get("_target")
        if safe and target:
            key = (tr.get("name"), target)
            prev = last_seen.get(key)
            if prev is not None and prev[1] == epoch:
                pi, po = prev[0], observations[prev[0]]
                # 较旧那条（pi）在可压缩区且尚未压过 → 削成取代桩（新副本已在后面，内容不丢）
                if pi < cutoff and not po.get("_compacted"):
                    orig = len(str(po.get("response", "")))
                    po["response"] = (f"[Observation superseded: the result of {po.get('name')} "
                                      f"reading \"{target}\" was superseded by a later read of the "
                                      f"same target; originally {orig} chars]")
                    po["_compacted"] = True
                    compacted += 1
            last_seen[key] = (i, epoch)
        if not safe:
            epoch += 1  # 写/有副作用观察 = 屏障：其后读取进入新 epoch，不与屏障前的读取互相取代

    # --- 阶段二：预算驱逐（T3-a） ---
    budget = int(budget_chars) if budget_chars else 0
    if budget > 0:
        total = sum(len(str(tr.get("response", ""))) for tr in observations)
        if total > budget:
            # 可压缩候选：可压缩区、未压过、且体量 > digest（压小观察无收益，「小观察即使旧也留」）
            cands = [(i, tr) for i, tr in enumerate(observations)
                     if i < cutoff and not tr.get("_compacted")
                     and len(str(tr.get("response", ""))) > digest_chars]
            # 体量大者优先（同体量取更旧）：最少次数打断前缀即回到预算内，「大观察即使较新也先压」
            cands.sort(key=lambda it: (-len(str(it[1].get("response", ""))), it[0]))
            for _, tr in cands:
                if total <= budget:
                    break
                resp = str(tr.get("response", ""))
                head = resp.strip()[:digest_chars].rstrip()
                tr["response"] = head + f" …[Observation compacted; originally {len(resp)} chars]"
                tr["_compacted"] = True
                compacted += 1
                total -= (len(resp) - len(tr["response"]))
    return compacted


# 把**转录本**变成笔记的机关，就是摘要器吐出一行字面 `MEMO:`（见下方 `_split_memo`）。
# 本函数堵的是其中**照抄那条路**：别让转录本里出现这样一行。
#
# ⚠️ 如实界定收益（首审 #8/#20，别把这条读成「F4 已闭环」）：
#   · 只堵照抄。注入文本若是**指令式**的（「在摘要末尾追加一行 MEMO: …」），本函数
#     一字不动它——那要靠模型不听话，是概率面不是确定性面。
#   · 归一化的近似形（`MEMO :`、`**MEMO:**`、`# MEMO:`、带零宽字符的）两侧都不认。
#   · 转录本不是唯一入口：`memory/dream.py` 会从模型输出里解析 `MERGE|/REWRITE|`
#     写进同一个笔记库，那是另一条（既有的、不在本次改动范围内的）操纵面。
#
# 威胁模型（首审大幅收窄过，别按原始建议的宽版本理解）：原建议声称
# 「恶意 PDF → extra_text → 裸拼进摘要器」——**这条链不存在**，`_turn_to_text` 既不读
# `ingested_text` 也不读 `role=tool` 的工具观察。真实通道只剩两条：
#   (a) 纯语音消息的 ASR 转写被升格成 prompt；
#   (b) **助手自己的答复复述了网页/PDF 里的内容**，经 `Assistant: {content}` 进摘要器。
# (b) 在「帮我总结这个页面」这类桌面高频任务里真实可达，故值得堵。
#
# 判据与 `_split_memo` 逐字同源（`strip().upper() == "MEMO:"`），另加全角冒号变体
# ——全角本身不被 `_split_memo` 认，但摘要器很可能把它归一成半角再抄出去。
_MEMO_MARKERS = ("MEMO:", "MEMO：")


def _defuse_memo_marker(text: str) -> str:
    """给转录本里「独占一行的 MEMO 标记」拆引信，其余内容一字不动。

    只改**整行就是标记**的那种行；`MEMO: 记得买牛奶` 这类带正文的不动——它本来就不会
    被 `_split_memo` 认成锚点，动它只会平白改写用户的原话。
    """
    if not text:
        return text
    lines = text.splitlines()
    hits = [i for i, ln in enumerate(lines) if ln.strip().upper() in _MEMO_MARKERS]
    if not hits:
        # ⚠️ 无标记时必须原样返回。`splitlines()/"\n".join()` 不是恒等变换：CRLF、
        # \x0b、\u2028 与尾随换行都会被悄悄改写，而这段文本的另一个下游是 rag_sink
        # 的「无损承重网」——那里要求与日志逐字节一致（首审 #13）。
        return text
    for i in hits:
        lines[i] = f"[quoted] {lines[i].strip()}"
    return "\n".join(lines)


def _turn_to_text(turn: Dict[str, Any]) -> str:
    """把一条已落盘轮次抽成供摘要的纯文本（用户输入 + 助手答复 + 用到的工具名）。

    头部盖轮次日期戳（时间锚定纪律，源自 mem0 评估）：这段文本有两个下游——
    摘要器（靠它把「明天/上周」换算成绝对日期）与 rag_sink 沉淀（agent_memory
    里的原文自带日期，几个月后召回仍知道「当时是哪天」）。相对时间不锚定，
    沉淀就是在存会过期的谜语。
    """
    parts: List[str] = []
    ts = turn.get("timestamp")
    if ts:
        try:
            parts.append(f"[{datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d %H:%M')}]")
        except Exception:
            pass
    prompt = str(turn.get("prompt", "")).strip()
    if prompt:
        parts.append(f"User: {prompt}")
    tools_used: List[str] = []
    for msg in turn.get("response", []) or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if content:
                parts.append(f"Assistant: {content}")
            for tc in msg.get("tool_calls", []) or []:
                nm = tc.get("name") or (tc.get("function") or {}).get("name")
                if nm:
                    tools_used.append(str(nm))
        elif msg.get("role") == "system" and msg.get("content"):
            parts.append(f"System: {msg['content']}")
    if tools_used:
        parts.append(f"(tools called this turn: {', '.join(tools_used)})")
    return _defuse_memo_marker("\n".join(parts))


# ---------- 零 LLM 沉淀：第二水位线 sedimented_turns（波 F5） ----------
# 病根：折叠曾是沉淀的**唯一**入口，而折叠要两个条件同时成立——上下文越过 token 阈值、
# 且日志行数 > keep_tail_turns（默认 4）。桌面 agent 的主导形态恰是 ≤4 轮的短会话：
# 两条都不满足 → 永不折叠 → 既不沉淀也不冲洗 → 30 天后整目录删除，**长期记忆产出恒为零**。
# 修法：沉淀本来就不需要模型（原样文本入 RAG），把它从折叠里解耦出来、单挂一条水位线。
# 冲洗（MEMO 抢救）确实要一次生成，故**不在本层**（审查已砍掉预热层）。
#
# 两条水位线的关系：`summarized_turns`「已并入滚动摘要」（要模型），`sedimented_turns`
# 「原文已进 RAG」（不要模型）。互不从属：沉淀通常跑在折叠前面，但配置关过沉淀再打开时
# 也可能落在后面。两条路径都以 `sedimented_turns` 为**唯一**权威，故同一段原文不会入库两次。

# 整条会话短到不值得占一条长期记忆（「你好」级）。**只在水位线还在 0 时管**：门槛是拿来
# 挡整条琐碎会话的，不是拿来卡实质会话的小尾巴——对后者卡住等于让「谢谢」那一轮永远沉不下去，
# 而且每个空闲回合都要为一次注定的空转重读整份日志（首审 #7）。
_SEDIMENT_MIN_CHARS = 200
# 单次沉淀的字符预算：积压很久（RAG 掉线过 / 沉淀关过再打开）时不许攒成一个巨型 POST，
# 那种请求在 10s 超时下大概率永远失败，水位线永远不推进，于是每 120s 重发一次整个积压
# （首审 #5）。分片让进度单调向前，代价只是多几个空闲回合。
_SEDIMENT_MAX_CHARS_PER_PASS = 100_000


def _read_log_lines(history_log: Path) -> Optional[List[str]]:
    """读历史日志的非空行。**读不动返回 None**，与「日志是空的」严格区分。

    两者绝不能混：调用方拿 `len(lines)` 去夹水位线，一次读盘失败若被当成「0 行」，
    水位线会被夹到 0——整条会话的原文随后重沉一遍。文件不存在才是真的 0 行。
    """
    if not history_log.exists():
        return []
    try:
        with open(history_log, "r", encoding="utf-8") as f:
            return [line for line in f if line.strip()]
    except Exception:      # OSError 之外还有 UnicodeDecodeError（坏行/半截写入）
        return None


def _collect_turn_texts(lines: List[str]) -> List[str]:
    """日志行切片 → 可入记忆的文本块。作废轮次（fail/cancelled）与空块剔除。

    作废轮次本就不进记忆（与折叠同一口径）：它们没产出，存进 RAG 只是污染召回。
    """
    out: List[str] = []
    for line in lines:
        try:
            turn = json.loads(line)
        except Exception:
            continue
        if turn.get("status") in ("fail", "cancelled"):
            continue
        text = _turn_to_text(turn)
        if text.strip():
            out.append(text)
    return out


def _sediment_title(session_id: str, start: int, end: int) -> str:
    """两条沉淀路径共用一种标题格式——检索面只容忍一种句柄。"""
    return (f"{session_id} · turns {start}-{end} · "
            f"{time.strftime('%Y-%m-%d')} · {int(time.time())}")


async def _sediment_range(session, lines: List[str], start: int, end: int,
                          *, min_chars: int = 0) -> bool:
    """沉淀日志行 [start, end) 的原文，**成功后**才推进第二水位线。返回是否推进。

    失败不推进（下次空闲回合原样重试）：这一层的存在理由就是「长期记忆产出不许静默为零」，
    RAG 掉线一小时就永久丢掉这段，等于把要修的病换个地方复发。代价是「已插入但响应丢失」
    时会留下一份重复——只增史料里的重复是噪声，静默丢失是缺陷，两害相权取噪声。
    """
    batch = _collect_turn_texts(lines[start:end])
    text = "\n\n---\n\n".join(batch)
    if not text.strip():
        # 区间内全是作废轮次：没有可存的原文，直接推进水位线（否则每回合都重扫同一段）
        session.sedimented_turns = end
        session.save_memory()
        return True
    if min_chars and len(text) < min_chars:
        return False
    if not await asyncio.to_thread(rag_sink.sediment, text,
                                   _sediment_title(session.session_id, start, end)):
        return False
    session.sedimented_turns = end
    session.save_memory()
    return True


def _budgeted_end(lines: List[str], start: int, max_chars: int) -> int:
    """在字符预算内挑本次沉淀的终点。至少含一行——否则超大单轮会把进度卡死。"""
    total = 0
    for i in range(start, len(lines)):
        total += len(lines[i])
        if total >= max_chars:
            return i + 1
    return len(lines)


async def sediment_pending_turns(session, *, enabled: bool = True,
                                 min_chars: int = _SEDIMENT_MIN_CHARS) -> int:
    """零 LLM 沉淀：把第二水位线之后的已落盘轮次原文送进 RAG。返回新沉淀的日志行数。

    不改 model-facing 上下文、不占生成位，故**无须保留尾部**（沉淀只是复制一份进 RAG，
    与折叠削上下文是两回事）。需在会话锁内调用（要写 memory.json）。
    """
    if not enabled:
        return 0
    lines = _read_log_lines(session.history_log)
    if lines is None:
        return 0                      # 日志读不动：什么都别做，尤其别夹水位线
    start = session.sedimented_turns
    if start > len(lines):
        # 水位线越过日志末尾：分支只带前缀（fork）、删末轮截断、或日志被重写。必须向下
        # 修复——否则后续新轮次一直落在水位线之下，这条会话从此再不沉淀（静默失效）。
        start = len(lines)
        session.sedimented_turns = start
        session.save_memory()
    if start >= len(lines):
        return 0
    end = _budgeted_end(lines, start, _SEDIMENT_MAX_CHARS_PER_PASS)
    # 门槛只挡「整条会话都琐碎」，故仅在水位线还在 0 时生效
    gate = min_chars if start == 0 else 0
    if not await _sediment_range(session, lines, start, end, min_chars=gate):
        return 0
    return end - start


def last_chance_sediment(session_dir: Path, session_id: str,
                         *, min_chars: int = _SEDIMENT_MIN_CHARS) -> Optional[bool]:
    """TTL 过期回收前的最后一次沉淀（同步、只读会话目录，不拉起 ChatSession）。

    **刻意只挂在 TTL 过期这一条路，不挂 delete_session**：用户手动删除会话是「忘掉它」的
    明确信号，在那条路上沉淀等于把用户刚删掉的东西搬进长期记忆。而 TTL 回收是磁盘回收策略，
    长会话早已把折叠掉的原文留在了 RAG 里，短会话在这里补齐只是把两者拉齐。

    返回 None=无事可做，True=已沉淀，False=沉淀失败（RAG 不可达）。不写回水位线——
    目录马上就要 rmtree；万一删除失败，下一轮至多多一份重复。
    """
    memo_start = 0
    mem_file = session_dir / "memory.json"
    if mem_file.exists():
        try:
            with open(mem_file, "r", encoding="utf-8") as f:
                mem = json.load(f)
            # 与 Session.load_memory 同一口径：老存档没有 sedimented_turns 键，取摘要
            # 水位线兜底。回落 0 会让每条老会话在过期时把已折叠部分整段重沉一遍。
            memo_start = int(mem.get("sedimented_turns",
                                     mem.get("summarized_turns", 0)))
        except Exception:
            memo_start = 0
    lines = _read_log_lines(session_dir / "history.jsonl")
    if lines is None or memo_start >= len(lines):
        return None
    text = "\n\n---\n\n".join(_collect_turn_texts(lines[memo_start:]))
    # 与 sediment_pending_turns 同一口径：门槛只挡整条琐碎会话。已沉淀过的会话
    # （memo_start > 0）说明它有实质内容，剩下的尾巴再短也要带走——这是最后一次机会。
    # 这里刻意不分片：目录马上 rmtree，分片只会让后面几片永远没机会。
    if not text.strip() or (memo_start == 0 and len(text) < min_chars):
        return None
    return bool(rag_sink.sediment(text, _sediment_title(session_id, memo_start, len(lines))))


# ---------- 压缩前记忆冲洗（memory flush，grok-build/mem0 评估合并票） ----------
# 摘要调用一次双产出：摘要正文 + 尾部 MEMO 抢救块（≤3 行「name | type | 事实句」）。
# 理念：折叠原文已由 rag_sink 无损沉淀（承重网），flush 的增量价值是把耐久事实
# **提升**到笔记层（有索引、随环境块注入、模型「知道自己知道」）。本地推理一次
# 调用很贵，故不另起 flush 回合而是搭摘要的车——mem0 抽取指引融进 SUMMARY_INSTRUCTION。

_FLUSH_MAX_NOTES = 3


def _split_memo(text: str) -> Tuple[str, List[str]]:
    """把摘要输出拆成（纯摘要, MEMO 行列表）。无 MEMO 块 = 原文照返、空列表。"""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        # 认全角变体（与 `_defuse_memo_marker` 同一份 `_MEMO_MARKERS`）：入口拆引信时
        # 收了全角、出口却不认的话，摘要器**真吐出**全角时这块既不会被剥出滚动摘要
        # （下次合并时 [Existing summary] 里冒出 MEMO 会教坏模型重复输出），抢救出的
        # 事实也会静默丢掉（首审 #7）。**锚点取第一个**这条语义不变。
        if ln.strip().upper() in _MEMO_MARKERS:
            memo = [l.strip() for l in lines[i + 1:] if l.strip().startswith("-")]
            return "\n".join(lines[:i]).rstrip(), memo
    return text, []


def _flush_rescued_notes(memo_lines: List[str], session_id: str) -> int:
    """把 MEMO 行写进笔记本（+ RAG 正文镜像）。全程 fail-open，返回成功条数。

    只增不覆盖（mem0 ADD-only 纪律）：名字已存在即跳过——自动冲洗的产出质量
    低于 agent 深思后的 memory_write，绝不能让它悄悄盖掉手写笔记；更新语义
    留给显式工具调用。格式/类型/slug 不合法的行静默跳过（模型智力维度，D1）。
    """
    from ..agents import runtime  # 惰性导入避免 memory↔agents 环
    store = runtime.get_note_store()
    if store is None:
        return 0
    written = 0
    for raw in memo_lines[:_FLUSH_MAX_NOTES]:
        parts = [p.strip() for p in raw.lstrip("-").strip().split("|", 2)]
        if len(parts) != 3:
            continue
        name, type_, fact = parts
        if type_ not in NOTE_TYPES or not fact:
            continue
        if store.get(name) is not None:
            continue  # 只增不覆盖
        desc = fact if len(fact) <= 120 else fact[:117] + "…"
        body = (f"{fact}\n\n（压缩前记忆冲洗自动抢救：会话 {session_id}，"
                f"{date.today().isoformat()}。如与手写笔记冲突，以手写为准。）")
        try:
            store.write(name, desc, type_, body)
        except Exception as e:
            print(f"[Flush] 抢救笔记 {name!r} 写入失败（跳过）: {e}")
            continue
        try:
            rag_sink.sediment_note(name, desc, type_, body)
        except Exception:
            pass  # RAG 镜像失败不影响：文件已落盘、索引已重建
        written += 1
    return written


async def maybe_summarize_context(session, engine, *, threshold_tokens: int,
                                  keep_tail_turns: int, last_prompt_tokens: Optional[int],
                                  batch_min: int = 1, sediment: bool = True) -> int:
    """
    若 model-facing 上下文（以 last_prompt_tokens 估）越过 threshold_tokens，则把最旧的、
    尾部保留之外的已落盘轮次折叠进滚动摘要，推进水位线并落盘，随后重建为 [system]+[摘要]+[尾部]。
    返回本次新折叠的轮次数（0 表示未触发或无可折叠）。需在会话锁内、轮次边界调用。
    """
    if not last_prompt_tokens or last_prompt_tokens < threshold_tokens:
        return 0
    if not session.history_log.exists():
        return 0
    lines = _read_log_lines(session.history_log)
    if lines is None:
        return 0
    fold_until = len(lines) - keep_tail_turns
    if fold_until - session.summarized_turns < batch_min:
        return 0  # 尾部保留够多，暂无足够旧轮次可折叠

    batch = _collect_turn_texts(lines[session.summarized_turns:fold_until])

    old_watermark = session.summarized_turns
    if not batch:
        # 待折叠区全是作废轮次：直接推进水位线避免反复重试
        session.summarized_turns = fold_until
        session.save_memory()
        session.rebuild_from_log()
        return fold_until - old_watermark

    batch_text = "\n\n---\n\n".join(batch)
    new_summary = await engine.summarize(
        batch_text, session.rolling_summary,
        cancel_check=lambda: bool(getattr(session, "cancel_requested", False)))
    # 压缩前记忆冲洗：剥出尾部 MEMO 抢救块（滚动摘要绝不能被它污染——
    # 下次合并时 [Existing summary] 里冒出 MEMO 会教坏模型重复输出）
    new_summary, memo_lines = _split_memo(new_summary)
    if not new_summary.strip():
        return 0  # 摘要为空（生成异常）：保持原样，下次再试

    session.rolling_summary = new_summary
    session.summarized_turns = fold_until
    session.save_memory()

    # 沉淀（Stage 3）：把被折叠掉的对话原文存入 RAG 长期记忆（无损承重网），
    # 供日后 rag_search（agent_memory 类目）召回。fail-open、在线程里做，绝不阻塞或影响压缩。
    # 起点取**第二水位线**而非摘要水位线：空闲回合的零 LLM 沉淀（F5）可能已经跑在前面，
    # 那段原文早就在库里了，照 old_watermark 重发一遍就是平白多一份重复。
    if sediment and session.sedimented_turns < fold_until:
        await _sediment_range(session, lines, session.sedimented_turns, fold_until)

    # 压缩前记忆冲洗落笔（fail-open，线程里做：写文件 + 重建索引 + RAG 镜像）
    if memo_lines:
        rescued = await asyncio.to_thread(
            _flush_rescued_notes, memo_lines, session.session_id)
        if rescued:
            print(f"[Flush] 压缩前抢救 {rescued} 条耐久事实入笔记本")

    # 重建 model-facing 上下文为 [system]+[摘要]+[尾部]；旧 KV 由下次推理前缀匹配自动对齐
    # （系统前缀命中复用，摘要块起始处发散、其后重新预填充——即"只保 system 前缀复用"）。
    session.rebuild_from_log()
    # T1：标脏——上下文已被改写，摘要块之后的重预填（M2 实测 2-4s）本会落在用户
    # 下一轮的等待路径上；空闲维护回合看到此标志会趁闲把新前缀预填回缓存
    session.context_dirty = True
    return fold_until - old_watermark
