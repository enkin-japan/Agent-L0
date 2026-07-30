"""文件式长期记忆：agent 主动撰写的笔记本（EXECUTION_LEDGER R4）。

**这一层补的是什么**：会话记忆（`compactor.py` + `rag_sink.sediment`）不粗糙——取代去重、
写屏障、预算驱逐都实现精细。缺的不是质量，是**一个层**：

| | 会话记忆（已有） | 文件记忆（本模块） |
|---|---|---|
| 尺度 | session 内 | **跨 session** |
| 作者 | 系统自动（摘要器） | **agent 主动撰写** |
| 内容 | 发生过什么 | **学到了什么** |
| 性质 | 被动/无损/全量 = 录像带 | 主动/提炼/有损 = **笔记本** |

`rag_sink` 是录像带：一柜子录像带，没有笔记本。

**两条召回路**（互补，不是冗余）：
- **索引进提示词**：`MEMORY.md` 的一行摘要随环境块注入主编排轮 → 模型**知道自己知道**。
  没有这条，模型压根不会想到去搜——笔记写了也白写。
- **正文走 RAG**：类目 `agent_notes`，经既有 `rag_search` 语义召回正文。
  （cc-haha 只能靠额外一次 Sonnet 调用扫全部 header 做选择；本项目有真向量库，
  这是 RAG 线的免费红利。）

**🔴 隐私红线**：笔记是个人事实。索引只随 `include_env=True` 注入 → **只进主编排轮**，
绝不进沙箱子生成（沿用 `prompt_builder` 的环境门控，同一条闸门，不另造）。
正文侧同理由构造保证：沙箱工具集里根本没有 `rag_search`（沙箱不变量 1）。

**落盘位置**：`backend/notes/`，与 `sessions/` `schedules/` `push/` 平级并同样 gitignore。
笔记是**用户的个人事实**，绝不能进代码树被提交上 GitHub。
"""
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from ..utils.frontmatter import parse_frontmatter

#: 四类型分类学（借鉴 cc-haha `memdir/memoryTypes.ts` 的**设计**，不抄代码）。
#: 分类不是为了归档整齐，是为了逼作者想清楚「这条到底是什么」——想不清楚的多半不该存。
NOTE_TYPES = ("user", "feedback", "project", "reference")

#: 索引注入提示词的护栏。笔记会越写越多，而索引在**每一轮**都占上下文；
#: 不设上限就是让记忆系统慢慢吃掉自己的可用窗口（e4b 尤其吃不消）。
MAX_INDEX_LINES = 40
MAX_INDEX_CHARS = 4000


class Note:
    """一条笔记 = 一个事实。

    valid_until（数据架构采纳波 R3，时间维度最小形态）：**事实生效终点**（ISO 日期），
    区别于 frontmatter `updated`（记录时间）——这就是双时间轴，只是不为它建表。
    只有明确带失效日的事实才填（合同到期/保单期间/密码轮换）；耐久事实留空。
    过期后索引行显式标注 [EXPIRED]——模型看到的是"已过期"，而非自信引用旧事实
    （个人记忆系统最高频事故：三年前的旧地址答得斩钉截铁）。
    """

    def __init__(self, name: str, description: str, type_: str, body: str,
                 source: Optional[Path] = None, valid_until: str = ""):
        self.name = name
        self.description = description
        self.type = type_
        self.body = body
        self.source = source
        self.valid_until = valid_until

    def expired(self) -> bool:
        return bool(self.valid_until) and self.valid_until < date.today().isoformat()


def _slug_ok(name: str) -> bool:
    """名字即文件名：只收小写 kebab-case，挡掉路径穿越与花名。

    强制小写不是洁癖：macOS/Windows 的文件系统大小写不敏感，`X.md` 与 `x.md` 是同一个
    文件，而内存里的 `_notes` 字典会当成两条——索引说有两条、磁盘上只有一条，
    后写的悄悄盖掉先写的。直接在入口挡掉。
    """
    return (bool(name) and not name.startswith("-")
            and all((c.isalnum() and not c.isupper()) or c == "-" for c in name))


class NoteStore:
    """笔记本：目录扫描加载 + 单事实写入 + 索引重建。

    照 `SkillRegistry` 的形状（目录扫描 + 名字索引 + 逐文件 try/except），但多一条写入面
    ——skill 是人写的、笔记是 agent 写的。
    """

    def __init__(self, notes_dir: Path):
        self.notes_dir = Path(notes_dir)
        self._notes: Dict[str, Note] = {}
        self._load_all()

    # ---------- 读 ----------

    def _load_all(self) -> None:
        self._notes.clear()
        if not self.notes_dir.exists():
            return
        for file in sorted(self.notes_dir.glob("*.md")):
            if file.name == "MEMORY.md":      # 索引是产物，不是笔记
                continue
            try:
                meta, body = parse_frontmatter(file.read_text(encoding="utf-8"), file)
                name = str(meta.get("name") or "").strip()
                description = str(meta.get("description") or "").strip()
                type_ = str(meta.get("type") or "").strip()
                if not name or not description:
                    raise ValueError("frontmatter 必须含 name 与 description")
                if type_ not in NOTE_TYPES:
                    raise ValueError(f"type 必须是 {NOTE_TYPES} 之一，得到 {type_!r}")
                if not body:
                    raise ValueError("正文为空（笔记正文就是它的全部价值）")
                valid_until = str(meta.get("valid_until") or "").strip()
                self._notes[name] = Note(name, description, type_, body, file,
                                         valid_until=valid_until)
            except Exception as e:
                # 一条写坏了不连累其他（照 MCPRegistry / SkillRegistry 的先例）
                print(f"[Notes] ⚠️ 加载笔记 {file.name} 失败: {e}")

    def get(self, name: str) -> Optional[Note]:
        return self._notes.get(name)

    def names(self) -> List[str]:
        return sorted(self._notes)

    def index_lines(self) -> List[str]:
        """注入提示词的索引：每条一行 `- name (type): description`。

        超出 MAX_INDEX_LINES / MAX_INDEX_CHARS 时**截断并明说**——宁可让模型知道
        "还有更多、去搜"，也不能悄悄丢掉一半让它以为这就是全部。
        """
        if not self._notes:
            return []

        def _row(n: Note) -> str:
            if n.expired():
                # 过期标注放行首级别显眼处：比默默照常列出诚实，比直接隐藏可核
                return (f"- {n.name} ({n.type}) [EXPIRED {n.valid_until} — verify before "
                        f"relying on it]: {n.description}")
            tail = f" (valid until {n.valid_until})" if n.valid_until else ""
            return f"- {n.name} ({n.type}){tail}: {n.description}"

        rows = [_row(n) for n in (self._notes[k] for k in self.names())]
        dropped = 0
        if len(rows) > MAX_INDEX_LINES:
            dropped = len(rows) - MAX_INDEX_LINES
            rows = rows[:MAX_INDEX_LINES]
        while rows and sum(len(r) + 1 for r in rows) > MAX_INDEX_CHARS:
            rows.pop()
            dropped += 1
        head = ["Your long-term notes (write with memory_write; "
                "search category 'agent_notes' with rag_search for the full text):"]
        if dropped:
            rows.append(f"- …and {dropped} more note(s) not listed here — "
                        "use rag_search on category 'agent_notes' to find them.")
        return head + rows

    # ---------- 写 ----------

    def write(self, name: str, description: str, type_: str, body: str,
              valid_until: str = "") -> Path:
        """写入/覆盖一条笔记并重建索引。校验不过就抛 ValueError（由工具层翻成给模型的话）。

        valid_until：事实生效终点（可选，ISO 日期）。与 frontmatter `updated`（记录时间）
        构成双时间轴；格式非法就地拒绝——错日期比没日期更毒（会在错的日子打上 EXPIRED）。
        """
        name = (name or "").strip()
        if not _slug_ok(name):
            raise ValueError(f"name {name!r} 非法：只接受小写 kebab-case"
                             "（小写字母/数字/连字符，如 prefers-terse-replies）")
        if type_ not in NOTE_TYPES:
            raise ValueError(f"type 必须是 {'/'.join(NOTE_TYPES)} 之一，得到 {type_!r}")
        description = (description or "").strip()
        body = (body or "").strip()
        if not description:
            raise ValueError("description 不能为空（它就是索引里那一行，模型靠它决定要不要细读）")
        if not body:
            raise ValueError("正文不能为空")
        valid_until = (valid_until or "").strip()
        if valid_until:
            try:
                date.fromisoformat(valid_until)
            except ValueError:
                raise ValueError(f"valid_until 必须是 ISO 日期（YYYY-MM-DD），得到 {valid_until!r}；"
                                 "耐久事实请直接省略该字段")

        self.notes_dir.mkdir(parents=True, exist_ok=True)
        path = self.notes_dir / f"{name}.md"
        vu_line = f"valid_until: {valid_until}\n" if valid_until else ""
        path.write_text(
            f"---\nname: {name}\ndescription: {description}\ntype: {type_}\n"
            f"updated: {date.today().isoformat()}\n{vu_line}---\n\n{body}\n",
            encoding="utf-8")
        self._notes[name] = Note(name, description, type_, body, path,
                                 valid_until=valid_until)
        self._write_index()
        return path

    def delete(self, name: str) -> bool:
        """删除一条笔记并重建索引（dream 整理专用；工具面有意不暴露删除）。"""
        note = self._notes.pop(name, None)
        if note is None:
            return False
        try:
            if note.source and note.source.exists():
                note.source.unlink()
        finally:
            self._write_index()
        return True

    def _write_index(self) -> None:
        """重建 `MEMORY.md`。它是**产物不是真相**——真相是各 md 文件本身，
        故每次全量重建而不做增量修补（增量修补是索引和事实分叉的经典来源）。"""
        lines = ["# 长期记忆索引", "",
                 "> 本文件由 memory_write 自动重建，请勿手改——真相是同目录下的各 md 文件。", ""]
        for t in NOTE_TYPES:
            group = [n for n in (self._notes[k] for k in self.names()) if n.type == t]
            if not group:
                continue
            lines.append(f"## {t}")
            lines.extend(
                f"- [{n.name}]({n.name}.md) — "
                + (f"⚠️已过期 {n.valid_until}｜" if n.expired() else
                   (f"有效至 {n.valid_until}｜" if n.valid_until else ""))
                + n.description
                for n in group)
            lines.append("")
        (self.notes_dir / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")
