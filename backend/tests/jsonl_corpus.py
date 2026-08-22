"""JSONL 坏行/断尾夹具构造器(供 jsonl 纪律相关测试复用)。

每个构造器返回**字节**(而不是写文件),测试自由拼接后一次写入——坏行的病灶是字节级的
(切在多字节字符中间、缺尾随换行、无法 utf-8 解码),用 str 拼接会在半路把它治好。
拼好的语料用 `write_corpus(path, parts)` 落盘。
"""
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

# 记录正文含中日文:本仓记录如此,且断尾必须切在 3 字节序列中间才复现真实病状
_TEXT = "记录正文：日本語のテキストと中文が混在する一行"


def record(index: int, text: str = "") -> Dict[str, Any]:
    """一条典型记录(内容无特别含义,只要求是 dict 且含多字节正文)。"""
    return {"index": index, "role": "user" if index % 2 == 0 else "assistant",
            "text": f"{_TEXT}#{index}{text}"}


def encode(obj: Any) -> bytes:
    """记录 → 行体字节(不含行尾换行)。"""
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def valid_lines(n: int, start: int = 0) -> bytes:
    """n 条有效记录(每条带尾随换行)。"""
    return b"".join(encode(record(i)) + b"\n" for i in range(start, start + n))


def valid_objs(n: int, start: int = 0) -> List[Dict[str, Any]]:
    """与 `valid_lines` 一一对应的期望对象序列。"""
    return [record(i) for i in range(start, start + n)]


def pseudo_newline_record(index: int = 904) -> Dict[str, Any]:
    """正文含「像换行的字符」的有效记录:U+2028 / U+0085 不是控制字符区,
    `ensure_ascii=False` 下原样落盘——它们是记录内容,不是行界。"""
    return record(index, "\u2028行分隔\u2029段分隔"
                          "\u0085NEL\u000b\u000c")


def pseudo_newline_line(index: int = 904) -> bytes:
    return encode(pseudo_newline_record(index)) + b"\n"


def blank_lines() -> bytes:
    """空行 + 纯空白行(空格/制表符)。"""
    return b"\n   \n\t\n"


def non_dict_line() -> bytes:
    """合法 JSON 但不是 dict——本仓口径下算坏行。"""
    return b"12\n"


def undecodable_line() -> bytes:
    """无法 utf-8 解码的一整行。"""
    return b"\xff\xfe\x00\x01 not-utf8\n"


def complete_no_newline(index: int = 900) -> bytes:
    """完整记录,只差尾随换行(唯一被容忍的尾部异常)。"""
    return encode(record(index))


def truncated_multibyte(index: int = 901, filler: str = "") -> bytes:
    """断在多字节字符**中间**的半截 JSON 尾(无尾随换行)。

    切点取正文中部某个 3 字节 UTF-8 序列的首字节之后 → 前缀既解码不了也解析不了。
    """
    data = encode(record(index, filler))
    cut = len(data) // 2
    while cut < len(data) and not (0xE0 <= data[cut] < 0xF0):
        cut += 1
    assert cut < len(data), "语料正文里没有 3 字节字符,断尾构造失败"
    out = data[:cut + 1]
    try:
        out.decode("utf-8")
    except UnicodeDecodeError:
        return out
    raise AssertionError("断尾夹具竟然能 utf-8 解码,构造失败")


def huge_truncated_multibyte(index: int = 902, size: int = 80 * 1024) -> bytes:
    """超过一个反向扫描块(64KB)、且中间不含任何换行的断尾,用于跨块反向扫。

    切点在整条记录的中部,故正文按 2×size 造,残片才有 ~size 字节。
    """
    return truncated_multibyte(index, filler="桁" * (2 * size // 3))


def huge_valid_line(index: int = 903, size: int = 80 * 1024) -> bytes:
    """超长有效记录(>64KB,带尾随换行)。"""
    return encode(record(index, "桁" * (size // 3))) + b"\n"


def write_corpus(path: Path, parts: Iterable[bytes]) -> Path:
    """把若干片字节按序写成一个文件(整体覆盖写)。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"".join(parts))
    return p
