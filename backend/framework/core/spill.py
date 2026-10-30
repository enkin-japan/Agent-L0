"""超长观察落盘 + 回取句柄（波 C1）。

单独成模块而不是留在 `orchestrator.py`：压缩层（`memory/compactor.py`）要用同一套
句柄正则把冷恢复后的路径捞回来，而 orchestrator 自己 import compactor——放在一起就是环。
`session` 参数刻意不标注类型：标了就要 import ChatSession，又把环绕回来。

病根：截断是纯字符级的头 2/3 + 尾 1/3，被砍掉的中段**在系统里任何一处都不存在**
（账本对 tool_result 还要再截一次，`ToolOutput.payload` 只服务渲染面、模型够不到）。
而中缝原本那句「re-fetch a narrower slice (page/start/end/limit args)」对桌面任务最
盛产的那几类超长观察——AX 树、shell 输出、剪贴板——**根本是空头支票**：它们压根不支持
「取更窄的一片」。于是模型要么重跑整个工具，要么就此丢掉中段。
修法：截断前把原文落到会话目录，中缝改成给一条**真能走通**的路。刻意**不新增元工具**
——`file_extract` 已经是免审批、可并发的只读工具，且对 text 支持 start/end 行区间，
正好是「按行窄取回」的现成能力；新增元工具要给稳定前缀加钱，还稀释注意力。
回取**调用**只有一种（file_extract + 行区间）；路径另给一个短句柄 `spill:<hash>` 作同一调用的
等价写法（模型抄长绝对路径会抄错，见 resolve_spill_ref），压缩层仍只认长路径那一串。
"""
import os
import re
from pathlib import Path
from typing import Optional, Tuple


_SPILL_DIRNAME = "spill"
# 单份落盘上限。原文已经在内存里了，落盘不是新增一个数量级的资源风险；但一个跑飞的
# `find /` 仍不该在会话目录里留下无界的一份，故给个上限，并**如实**在中缝里说清只存了多少。
_SPILL_MAX_CHARS = 4_000_000
# 单会话落盘总预算。`prune_expired_sessions` 对 `schedule_*` 与 pinned 会话**一律跳过**，
# 那两类的 spill 目录没有任何回收路径——一个每天跑的定时任务会永远往同一个目录里堆。
# 超预算就不再落盘（中缝如实说没保存），**绝不删已经许诺过的文件**：删掉等于把句柄变成
# 死路，而死路正是这条改动要消灭的东西。
_SPILL_SESSION_BUDGET_BYTES = 128 * 1024 * 1024
# 中缝建议的回取预算 = 观察上限 − 余量。`file_extract` 文本分支缺省 max_chars=12000，
# 是观察上限的三倍：模型照缺省调一次，回来的东西**又会被截断并再落一次盘**（新的 call_id
# = 新路径，连重复调用阻断器都拦不住，因为参数变了），一个计划步的自纠预算就这么烧光。
# 这与 `file_extract._PDF_MAX_CHARS = 3400` 是同一笔账：工具级预算必须留出余量，让路标
# 活过编排层截断。默认 cap=4000 时这里正好也是 3400。
_SPILL_READ_MARGIN = 600
# call_id 是编排层自己 `uuid4().hex[:8]` 生成的（不是模型给的），路径穿越面本就不存在。
# 这条断言只当纵深，别把它写成主要安全面。
_CALL_ID_RE = re.compile(r"^[0-9a-f]{1,16}$")
# 冷恢复兜底用：`_` 前缀元数据落盘时被剥，重载会话后压缩桩要从观察正文里把句柄捞回来。
_SPILL_CALL_RE = re.compile(r'file_extract\(path="[^"]+", start=\d+, end=\d+, max_chars=\d+\)')
_SPILL_TOTAL_RE = re.compile(r"\((\d+) chars total\)")
# 落盘文件名（不含 .txt）：`<call_id>` 或撞名改名后的 `<call_id>-<n>`（见 spill_observation）
_SPILL_STEM_RE = re.compile(r"^[0-9a-f]{1,16}(?:-[0-9]{1,2})?$")
# 短句柄 `spill:<stem>`：模型抄长绝对路径会抄错（实案 app-9daf9c9fee44：`dev.enkin.macagent`
# 两次抄成 `dev.inkin` / `dev.injin`，回取全挂）。短句柄只在**当前会话**的 spill 目录内解析。
SPILL_HANDLE_PREFIX = "spill:"


def spill_handle(path: str) -> str:
    """落盘绝对路径 → 短句柄 `spill:<stem>`（中缝与 file_extract 共用这一种拼法）。"""
    return SPILL_HANDLE_PREFIX + Path(path).stem


def resolve_spill_ref(session, path: str) -> Optional[Path]:
    """把短句柄 / 抄错的落盘路径解析到**当前会话** spill 目录里的真实文件；不成立返回 None。

    两种入口：① `spill:<stem>`（可带 `.txt`）；② 绝对路径不存在、但路径里有 `/spill/` 且
    文件名形如落盘名（`<hex>.txt` / `<hex>-<n>.txt`）——按文件名在当前会话 spill 目录重试。
    fail-closed：文件名必须过落盘名正则，且 realpath 必须落在当前会话 spill 目录的 realpath
    之内（符号链接出逃、`spill:../x` 一律拒）；任何一条不成立都返回 None，调用方照旧报
    not found——绝不把会话外的文件当成句柄目标。
    """
    if session is None or getattr(session, "session_dir", None) is None:
        return None
    raw = str(path or "").strip()
    if raw.startswith(SPILL_HANDLE_PREFIX):
        name = raw[len(SPILL_HANDLE_PREFIX):]
        if name.endswith(".txt"):
            name = name[:-4]
    else:
        if f"/{_SPILL_DIRNAME}/" not in raw or not raw.endswith(".txt"):
            return None
        name = raw.rsplit("/", 1)[-1][:-4]
    if not _SPILL_STEM_RE.fullmatch(name):
        return None
    try:
        root = os.path.realpath(Path(session.session_dir) / _SPILL_DIRNAME)
        target = os.path.realpath(os.path.join(root, f"{name}.txt"))
        if os.path.commonpath([root, target]) != root or target == root:
            return None
        return Path(target) if os.path.isfile(target) else None
    except (OSError, ValueError):
        return None


def _spill_dir_bytes(d: Path) -> int:
    try:
        return sum(f.stat().st_size for f in d.iterdir() if f.is_file())
    except OSError:
        return 0


def spill_observation(session, call_id: str, text: str) -> Optional[Tuple[str, int]]:
    """把超长观察的原文落到 `sessions/<sid>/spill/<call_id>.txt`，返回 (绝对路径, 已写字符数)。

    写不成就返回 None——**绝不让写失败冒充成功**：给模型一个不存在的路径比不给更毒，
    它会照着调一次 `file_extract`、拿回 not found，然后要么重跑整个工具要么放弃。
    后缀取 `.txt` 是让 `file_extract` 走文本分支（它按后缀分流，`.pdf`/音频/office 各有
    专属分支；未识别后缀也会落到文本分支，但显式写 `.txt` 才是自说明的）。
    权限 0600：落进来的恰恰是最敏感的那一类观察——shell 输出（`env`、被要求查看的 `.env`）、
    剪贴板、显示着令牌的窗口 AX 树。会话目录的既有文件是 0644，这一类不跟那个惯例。
    目录随会话删除自然回收（`delete_session` / `prune_expired_sessions` 都是整棵 rmtree）。
    """
    if not _CALL_ID_RE.match(str(call_id or "")):
        return None
    try:
        d = session.session_dir / _SPILL_DIRNAME
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        if _spill_dir_bytes(d) >= _SPILL_SESSION_BUDGET_BYTES:
            print(f"[Orchestrator] 会话 {session.session_id} 落盘预算已满，本条不落盘")
            return None
        p = d / f"{call_id}.txt"
        # call_id 只有 32 bit，撞了就会用另一条观察的内容覆盖这一条——**有效路径配错误
        # 内容比 not found 更坏**（没有任何信号）。撞上就换个名字，别覆盖。
        for n in range(1, 8):
            if not p.exists():
                break
            p = d / f"{call_id}-{n}.txt"
        body = text[:_SPILL_MAX_CHARS]
        p.write_text(body, encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        return str(p), len(body)
    except Exception as e:
        print(f"[Orchestrator] 超长观察落盘失败（中缝将不给路径）: {e}")
        return None


def truncate_observation(text: str, cap: int, spill: Optional[Tuple[str, int]]) -> str:
    """保头 2/3 + 尾 1/3，中缝报原始体积并给回取句柄。

    句柄**只有中缝这一份**：压缩层要用时从这段正文里正则捞回去（`_SPILL_CALL_RE`），
    不另挂一份内存元数据。两处各存一份就是两个真相源——而其中一份（`_` 前缀键）落盘时
    会被剥掉，冷恢复后必然分家。少一个源，就少一次分家。

    保尾的理由（grok-build 评估采纳）：shell/日志类输出的错误行多在尾部，只保头会把最
    有用的部分砍掉。中缝三条语义缺一不可——**报原始体积**（否则模型不知道自己漏了多少）、
    **给可行动的出路**（S.3：给路不给谜语）、以及**出路走不通时如实说走不通**。

    行号是算出来的具体整数，不是 `<line>` 占位符：占位符会被原样抄进调用，
    `int("<line>")` 抛异常、registry 把整条 traceback 甩给模型。算行号用 `splitlines()`，
    与 `read_text_range` 逐字同一套切分语义（它也认 \\r、\\x0b、U+2028），否则模型按我们
    报的行号去取，取到的是另一段。
    """
    total = len(text)
    if cap <= 0:
        return text          # cap≤0 = 关掉截断（tail_n=0 时 text[-0:] 会返回全文）
    head_n = cap * 2 // 3
    tail_n = cap - head_n
    head, tail = text[:head_n], text[-tail_n:]
    if not spill:
        seam = (f"\n...[observation truncated: showing the first {head_n} and last {tail_n} "
                f"of {total} chars ({total} chars total). The omitted middle was NOT saved "
                f"anywhere; if you need it, re-fetch a narrower slice from the source]...\n")
        return head + seam + tail

    path, saved = spill
    n_lines = len(text.splitlines())
    head_end = max(1, len(head.splitlines()))
    tail_start = max(head_end, n_lines - len(tail.splitlines()) + 1)
    kept = ("" if saved >= total else f" (only the first {saved} chars fit the per-file cap)")
    if n_lines <= 1:
        # 单行载荷（剪贴板整段、压缩过的 JSON/HTML、base64）——按行取回**根本不成立**，
        # `read_text_range` 会回 `[Empty range: file has 1 lines]`。这时给行号句柄就是把
        # 旧那句空头支票换了个更自信的说法，故如实说走不通，并且**不禁止重跑工具**：
        # 那是这种载荷仅剩的退路。
        seam = (f"\n...[observation truncated: showing the first {head_n} and last {tail_n} "
                f"of {total} chars ({total} chars total). The full output was saved verbatim to "
                f"{path} (short handle: path=\"{spill_handle(path)}\"){kept}, but it has no "
                f"line breaks, so a line range cannot address the "
                f"omitted middle — narrow the query at the source instead]...\n")
        return head + seam + tail

    # 预算必须 ≤ 本次观察上限，否则回来的东西又撞一次截断（小 cap 的测试档尤其明显）
    budget = (cap - _SPILL_READ_MARGIN if cap > 2 * _SPILL_READ_MARGIN
              else max(1, cap // 2))
    handle = (f'file_extract(path="{path}", start={head_end}, end={tail_start}, '
              f'max_chars={budget})')
    seam = (f"\n...[observation truncated: showing lines 1-{head_end} and "
            f"{tail_start}-{n_lines} of {n_lines} lines ({total} chars total). "
            f"The full output was saved verbatim{kept}; read the omitted middle with "
            f"{handle} — or the same call with the short handle path=\"{spill_handle(path)}\" "
            f"instead of the long path. Do NOT re-run this tool just to see it again]...\n")
    return head + seam + tail
