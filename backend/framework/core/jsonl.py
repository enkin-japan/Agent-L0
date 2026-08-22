"""追加型 JSONL 的坏行纪律与断尾修复(本仓所有 *.jsonl 读写的唯一口径)。

约束(顺序即优先级):
1. **下标口径唯一**。`Record.index` 从 0 起、只数**有效记录**——坏行/空行不占号。
   前端下标、内存重放、turn_index、水位线全部按这一个定义对齐;分支克隆与删末轮
   按下标定位,任何一路自己数一套就会错位。
2. **写前先修尾**。进程崩溃会在文件尾留下半截 JSON(且可能切在 UTF-8 多字节字符
   中间);重启后的第一件事又是往同一文件追加恢复记录——不修尾就把残字节和新记录
   粘成一行垃圾,一条坏行同时污染上面四路口径。`append_record` 因此内建 `repair_tail`。
3. **坏内容永不抛,I/O 错误原样抛**。「文件是空的」与「读不动」必须可区分:上游各有
   自己的 try 纪律,把 OSError 吞成空迭代会让磁盘故障表现为「会话历史没了」。
4. **绝不用 splitlines()**。它把 \\r / \\x0b / \\x0c / U+0085 / U+2028 也当行界,而
   本仓记录正文里出现这些字符是常态——行界只有 b"\\n" 一个。二进制读、按 b"\\n" 切。

坏行是仪表数据不是异常:跳过 + 每路径每类至多告警一次 + 计入 `bad_line_stats`
(累计数始终准确,只是不重复刷屏)。`strict=True` 反过来——坏行抛 ValueError。
⚠️ strict **只供离线校验与测试**,绝不进生产读者:各读者对异常的既有处置互不相同
(吞成 None/0/上抛),strict 一开「口径唯一」当场破裂。故它只是显式参数,没有
config 旋钮(首审 M7:全局旋钮会让四个读者四种行为)。
"""
import json
import os
import threading
from collections import namedtuple
from pathlib import Path
from typing import Any, Dict, Iterator, Union

# 反向扫描块大小:尾片可以远大于一块(超长记录),故按块向前找,不是只读末块
_BLOCK = 64 * 1024

# index: 0 起、只数有效记录;obj: dict(保证);raw: 该行原文(str,不含行尾 \n)
Record = namedtuple("Record", ["index", "obj", "raw"])

PathArg = Union[str, Path]

# 每路径累计坏行数(含断尾);仪表/测试直读
bad_line_stats: Dict[str, int] = {}

_LOCK = threading.Lock()
# 告警去重键 (path, kind):普通坏行与断尾各自至多一次——断尾是唯一可操作的那条
# (「该跑 repair_tail 了」),不能被同文件里先出现的普通坏行把名额吃掉。
_warned: set = set()

_KIND_BAD = "bad"
_KIND_TRUNCATED = "truncated"


def reset_stats() -> None:
    """清空坏行计数与告警去重状态(测试用;生产进程内单调累计)。"""
    with _LOCK:
        bad_line_stats.clear()
        _warned.clear()


def _note_bad(key: str, kind: str, message: str) -> int:
    """计数 + 首次告警。返回该路径的累计坏行数。"""
    with _LOCK:
        n = bad_line_stats.get(key, 0) + 1
        bad_line_stats[key] = n
        first = (key, kind) not in _warned
        if first:
            _warned.add((key, kind))
    if first:
        print(f"[jsonl] {message}(该文件此类仅告警一次,累计见 bad_line_stats;"
              f"当前累计 {n} 条)")
    return n


def _is_record(blob: bytes) -> bool:
    """尾片/行体是否为一条完整记录:utf-8 可解 + JSON 可解 + 是 dict。

    `b"12"` 这类合法但非 dict 的 JSON 也判否——本仓记录全是 dict,它只可能是断尾残片。
    """
    try:
        obj = json.loads(blob.decode("utf-8"))
    except Exception:
        return False
    return isinstance(obj, dict)


def iter_records(path: PathArg, *, strict: bool = False) -> Iterator[Record]:
    """逐条产出有效记录。文件不存在 → 空迭代;I/O 错误在首次迭代时原样抛出。

    唯一被容忍的尾部异常是「最后一行没有尾随换行」:能解析成 dict 就照常产出
    (完整记录只差个换行),否则算断尾——与普通坏行分开告警,措辞指明该修尾。
    """
    key = str(Path(path))
    try:
        f = open(path, "rb")
    except (FileNotFoundError, NotADirectoryError):
        # NotADirectoryError = 路径中段是文件(目录形态坏了),对读者与「不存在」同义
        # ——改前各读者靠 exists() 挡成空,不能因换口径让它一路炸到路由层(首审 L14)
        return
    with f:
        index = 0
        lineno = 0
        for chunk in f:                       # 二进制迭代只按 b"\n" 切,无 universal newline
            lineno += 1
            is_tail = not chunk.endswith(b"\n")   # 不带换行只可能是文件最后一片
            body = chunk if is_tail else chunk[:-1]
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as e:
                if strict:
                    _bump_strict(key)
                    raise ValueError(f"{key} 第 {lineno} 行 utf-8 解码失败: {e}")
                if is_tail:
                    _note_bad(key, _KIND_TRUNCATED,
                              f"{key} 末尾是断尾(半截 JSON,无尾随换行)已跳过,"
                              f"追加前请先 repair_tail")
                else:
                    _note_bad(key, _KIND_BAD, f"{key} 第 {lineno} 行 utf-8 解码失败,已跳过")
                continue
            if not text.strip(" \t\r"):
                continue                       # 空行/纯空白行:静默跳过,不占 index 不计数
                                               # (只认 ASCII 空白——U+2028/U+0085 等是记录
                                               #  内容不是行界,整行只有它们时按坏行计,首审 L12)
            try:
                obj = json.loads(text)
                ok = isinstance(obj, dict)
            except Exception:
                ok = False
            if not ok:
                if strict:
                    _bump_strict(key)
                    raise ValueError(f"{key} 第 {lineno} 行不是一条 JSON 对象记录: {text[:200]!r}")
                if is_tail:
                    _note_bad(key, _KIND_TRUNCATED,
                              f"{key} 末尾是断尾(半截 JSON,无尾随换行)已跳过,"
                              f"追加前请先 repair_tail")
                else:
                    _note_bad(key, _KIND_BAD, f"{key} 第 {lineno} 行不是合法 JSON 对象,已跳过")
                continue
            yield Record(index=index, obj=obj, raw=text)
            index += 1


def _bump_strict(key: str) -> None:
    """strict 抛错前也要计数:`bad_line_stats` 在两种模式下口径一致。"""
    with _LOCK:
        bad_line_stats[key] = bad_line_stats.get(key, 0) + 1


def count_records(path: PathArg, *, strict: bool = False) -> int:
    """有效记录数。直接复用 iter_records,保证与下标口径不会各自演化。"""
    return sum(1 for _ in iter_records(path, strict=strict))


def _find_last_newline(f, size: int) -> int:
    """最后一个 b"\\n" **之后**的字节位置;全文无换行则 0。

    从尾向前按块扫(尾片可能远超一块)。找的是单字节 0x0A,它不会出现在 UTF-8
    多字节序列内部,故块边界无需重叠、切点天然落在字符边界上。
    """
    pos = size
    while pos > 0:
        start = max(0, pos - _BLOCK)
        f.seek(start)
        block = f.read(pos - start)
        idx = block.rfind(b"\n")
        if idx >= 0:
            return start + idx + 1
        pos = start
    return 0


def repair_tail(path: PathArg) -> int:
    """保证下一次追加从新行开始,且绝不吞掉任何有效记录。返回被截掉的字节数(0=无事)。

    三种结局:末字节已是换行 → 0;尾片是完整记录只差换行 → 补一个 b"\\n" 封口、返回 0
    (零数据损失);尾片是断尾 → 截到最后一个换行之后。截断前重做一次尾片快照,与首读
    不一致说明有并发写者正在写,此时什么都不做(宁可不修,不可截掉别人正在写的记录)。
    幂等:修过再修返回 0。I/O 错误原样上抛。
    ⚠️ 稳定性检查只**窄化**并发窗口,不消除(快照与 truncate 之间无锁,首审 M5):
    它防的是「进程内另一线程正在追加」这类毫秒级重叠;两个进程写同一文件不在
    本模块的保护范围——那是部署红线(单后端进程),不是这里能兜的。
    """
    p = Path(path)
    try:
        f = open(p, "rb")
    except FileNotFoundError:
        return 0
    with f:
        size = os.fstat(f.fileno()).st_size
        if size == 0:
            return 0
        f.seek(size - 1)                       # 快路径:只读末字节,不读全文件
        if f.read(1) == b"\n":
            return 0
        cut = _find_last_newline(f, size)
        f.seek(cut)
        tail = f.read(size - cut)
        if _is_record(tail):
            if os.fstat(f.fileno()).st_size == size:
                with open(p, "ab") as w:
                    w.write(b"\n")
            return 0
        size2 = os.fstat(f.fileno()).st_size
        f.seek(cut)
        tail2 = f.read(max(0, size2 - cut))
        if size2 != size or tail2 != tail:
            return 0                           # 并发写者在写,让它写完
    with open(p, "r+b") as w:
        w.truncate(cut)
    dropped = size - cut
    print(f"[jsonl] 断尾修复:{p} 截掉尾部 {dropped} 字节(崩溃残留的半截记录;"
          f"其前所有有效记录逐字节保留)")
    return dropped


def append_record(path: PathArg, obj: Any, *, default=str) -> None:
    """追加一条记录。先修尾再写——单写者纪律下「残字节 + 新记录」粘行由此绝迹;
    repair_tail 因并发检测让路的那次追加仍可能粘(见其 docstring),读侧坏行纪律兜底。

    父目录不存在时按 open 的原样报错向上抛(与既有写点一致:会话已销毁就不该被
    一次残留写入半复活)。
    """
    p = Path(path)
    repair_tail(p)
    line = json.dumps(obj, ensure_ascii=False, default=default)
    with open(p, "ab") as f:
        f.write(line.encode("utf-8") + b"\n")
