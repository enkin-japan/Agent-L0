"""FTS 关键词腿（数据架构采纳波 R2）：与 Chroma 并置的 SQLite FTS5 索引。

为什么需要它（评估集 identifier/name 形态的结构性短板）：向量对精确 token 几乎无
区分度（标识符/人名/黑话被摊平成语义噪声）；既有 `extract_identifiers` contains 预取
只救「长得像标识符」的 token，救不了中文人名、缩写、口语黑话。FTS 是完整的关键词腿。

设计要点：
- **CJK bigram 预分词**：FTS5 内置 tokenizer 不切中文。入索引与查询用同一套
  `segment_for_fts`——CJK 连串展开为二元组序列（单字连串保单字），ASCII/数字词
  保词并小写。两侧同变换，unicode61 词表下即可精确匹配。
- **候选并集，不做 RRF 分数融合**：本架构已有终局裁判（重排器）。FTS 命中只
  「扩池不定序」，与 identifier 预取同位——bm25 只用来截前 N，最终排序恒归重排。
- **跨库检索，不受软路由约束**：关键词命中是强信号，正是软路由漏路时的兜底
  （体检指标在「健康」库、问题被路由去「设备」库的形态）。meta 路由库不入索引。
- **派生层纪律**：本索引可随时删除重建（删 fts_index.sqlite3 + reembed 全量重灌）。
  一切写操作 fail-open：FTS 故障绝不阻断入库/检索主流程，Chroma 仍是唯一真相。
"""
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger("RAG_FTS")

# CJK 统一表意 + 扩展A + 兼容表意 + 日文假名（黑话/外来词常以假名出现）
_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿぀-ヿ]")
_WORD_RE = re.compile(r"[A-Za-z0-9_./\-]+|[一-鿿㐀-䶿豈-﫿぀-ヿ]+")


def segment_for_fts(text: str) -> str:
    """入索引/查询共用的预分词：CJK 连串 → 二元组序列；ASCII 词保词小写。

    例：「王秀兰是房东」→ "王秀 秀兰 兰是 是房 房东"；"ERR_5023 报错" → "err_5023 报错"
    （ASCII 词内的 _-./ 保留——标识符是不可分割的原子）。
    """
    out: List[str] = []
    for tok in _WORD_RE.findall(text or ""):
        if _CJK_RE.match(tok):
            if len(tok) == 1:
                out.append(tok)
            else:
                out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
        else:
            out.append(tok.lower())
    return " ".join(out)


def build_match_query(*texts: str, max_terms: int = 24) -> str:
    """把查询文本编译成 FTS5 MATCH 串：去重词项 OR 连接（宽召回，bm25 定序截断）。

    词项引号包裹（"err_5023" 含 . / - 等 FTS 语法字符必须转义为字符串词元）。
    """
    seen: set = set()
    terms: List[str] = []
    for text in texts:
        for term in segment_for_fts(text).split():
            if term not in seen:
                seen.add(term)
                terms.append(term)
    terms = terms[:max_terms]
    return " OR ".join(f'"{t}"' for t in terms)


class FtsIndex:
    """chunks 关键词索引（FTS5 表 + 少量 UNINDEXED 定位列）。写操作全部 fail-open。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._local = threading.local()
        try:
            with self._conn() as c:
                c.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                    "chunk_id UNINDEXED, db UNINDEXED, source_path UNINDEXED, body, "
                    "tokenize='unicode61')")
            self.available = True
        except Exception as e:
            logger.warning(f"⚠️ FTS 索引初始化失败（关键词腿停用，检索退回向量+预取）: {e}")
            self.available = False

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return self._local.conn

    def add_chunks(self, db: str, ids: List[str], texts: List[str], source_path: str) -> None:
        if not self.available:
            return
        try:
            with self._conn() as c:
                c.executemany(
                    "INSERT INTO chunks_fts (chunk_id, db, source_path, body) VALUES (?,?,?,?)",
                    [(i, db, source_path, segment_for_fts(t)) for i, t in zip(ids, texts)])
        except Exception as e:
            logger.warning(f"⚠️ FTS 入索引失败（fail-open，向量索引不受影响）: {e}")

    def delete_ids(self, ids: List[str]) -> None:
        if not self.available or not ids:
            return
        try:
            with self._conn() as c:
                c.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", [(i,) for i in ids])
        except Exception as e:
            logger.warning(f"⚠️ FTS 删除失败（fail-open）: {e}")

    def delete_where_source(self, source_path: str) -> None:
        if not self.available:
            return
        try:
            with self._conn() as c:
                c.execute("DELETE FROM chunks_fts WHERE source_path = ?", (source_path,))
        except Exception as e:
            logger.warning(f"⚠️ FTS 按源删除失败（fail-open）: {e}")

    def drop_db(self, db: str) -> None:
        if not self.available:
            return
        try:
            with self._conn() as c:
                c.execute("DELETE FROM chunks_fts WHERE db = ?", (db,))
        except Exception as e:
            logger.warning(f"⚠️ FTS 删库失败（fail-open）: {e}")

    def search(self, *texts: str, limit: int = 10) -> List[Dict[str, str]]:
        """bm25 序前 N 命中。返回 [{chunk_id, db, source_path}]；失败返回空（fail-open）。"""
        if not self.available:
            return []
        match = build_match_query(*texts)
        if not match:
            return []
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT chunk_id, db, source_path FROM chunks_fts "
                    "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
                    (match, limit)).fetchall()
            return [{"chunk_id": r[0], "db": r[1], "source_path": r[2]} for r in rows]
        except Exception as e:
            logger.warning(f"⚠️ FTS 查询失败（fail-open，本路返回空）: {e}")
            return []
