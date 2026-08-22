"""JSONL 坏行纪律与断尾修复的金测试(framework/core/jsonl.py 的契约)。

负向用例一律配同文件同替身的阳性对照:证明「病能被复现」与「药确实治了病」是两件事,
少一半就只证明了断言不会红。
"""
import builtins
import json
import os

import pytest

from framework.core import jsonl
from framework.core.jsonl import (append_record, bad_line_stats, count_records,
                                  iter_records, repair_tail, reset_stats)

from . import jsonl_corpus as corpus


@pytest.fixture(autouse=True)
def _clean_stats():
    """坏行计数与告警去重是模块级状态,逐测试清零。"""
    reset_stats()
    yield
    reset_stats()


def test_mixed_file_index_and_count_agree(tmp_path):
    """混合坏行文件:index 连续 0..K-1、count 同口径、有效记录逐条正确。"""
    p = corpus.write_corpus(tmp_path / "mixed.jsonl", [
        corpus.valid_lines(1, start=0),
        corpus.blank_lines(),
        corpus.undecodable_line(),
        corpus.valid_lines(1, start=1),
        corpus.non_dict_line(),
        corpus.valid_lines(1, start=2),
        corpus.truncated_multibyte(),
    ])
    recs = list(iter_records(p))
    assert [r.index for r in recs] == [0, 1, 2]
    assert count_records(p) == len(recs) == 3
    assert [r.obj for r in recs] == corpus.valid_objs(3)
    assert [json.loads(r.raw) for r in recs] == corpus.valid_objs(3)


def test_pseudo_newlines_inside_a_record_are_not_line_breaks(tmp_path, capsys):
    """行界只有 b"\\n":正文里的 U+2028 / U+2029 / U+0085 / \\x0b / \\x0c 不切行。"""
    p = corpus.write_corpus(tmp_path / "u2028.jsonl", [
        corpus.valid_lines(1, start=0),
        corpus.pseudo_newline_line(),
        corpus.valid_lines(1, start=1),
    ])
    recs = list(iter_records(p))
    assert [r.index for r in recs] == [0, 1, 2]
    assert recs[1].obj == corpus.pseudo_newline_record()
    assert capsys.readouterr().out == ""
    assert bad_line_stats == {}


def test_bad_lines_skipped_warned_once_and_counted(tmp_path, capsys):
    """坏行:跳过 + 每类至多告警一次 + 计数准确(两条同类坏行只出一条告警)。"""
    p = corpus.write_corpus(tmp_path / "bad.jsonl", [
        corpus.valid_lines(1, start=0),
        corpus.undecodable_line(),
        corpus.non_dict_line(),
        corpus.valid_lines(1, start=1),
    ])
    assert len(list(iter_records(p))) == 2
    out = capsys.readouterr().out
    assert out.count("[jsonl]") == 1
    assert bad_line_stats[str(p)] == 2

    list(iter_records(p))                      # 再读一遍:计数照增,告警不再刷屏
    assert capsys.readouterr().out == ""
    assert bad_line_stats[str(p)] == 4


def test_clean_file_is_silent(tmp_path, capsys):
    """阳性对照:干净文件零告警、零计数、全量产出。"""
    p = corpus.write_corpus(tmp_path / "clean.jsonl", [corpus.valid_lines(5)])
    assert [r.obj for r in iter_records(p)] == corpus.valid_objs(5)
    assert capsys.readouterr().out == ""
    assert bad_line_stats == {}


def test_complete_record_without_trailing_newline(tmp_path, capsys):
    """完整记录只差尾随换行:照常产出、照常计数;repair_tail 只补换行且返回 0。"""
    parts = [corpus.valid_lines(2), corpus.complete_no_newline(900)]
    p = corpus.write_corpus(tmp_path / "nonl.jsonl", parts)
    before = p.read_bytes()

    recs = list(iter_records(p))
    assert len(recs) == 3 and count_records(p) == 3
    assert recs[2].obj == corpus.record(900)
    assert capsys.readouterr().out == ""
    assert bad_line_stats == {}

    assert repair_tail(p) == 0
    assert p.read_bytes() == before + b"\n"    # 零数据损失
    assert count_records(p) == 3


def test_multibyte_truncated_tail_is_cut_at_last_newline(tmp_path):
    """多字节断尾:截到最后一个换行之后,其前字节逐字节不变,返回值=被截字节数。"""
    head = corpus.valid_lines(3)
    tail = corpus.truncated_multibyte()
    p = corpus.write_corpus(tmp_path / "trunc.jsonl", [head, tail])
    assert count_records(p) == 3               # 断尾被跳过,不计入有效记录

    dropped = repair_tail(p)
    assert dropped == len(tail)
    assert p.read_bytes() == head              # 前缀逐字节不变
    assert count_records(p) == 3


def test_non_dict_tail_is_truncated(tmp_path):
    """`b"12"` 尾片:json 解析得出但不是 dict → 断尾,照截不留。"""
    head = corpus.valid_lines(2)
    p = corpus.write_corpus(tmp_path / "tail12.jsonl", [head, b"12"])
    assert repair_tail(p) == 2
    assert p.read_bytes() == head


def test_repair_before_append_prevents_glued_line(tmp_path):
    """粘行时序:断尾文件上先修尾再追加 → N+1 条有效记录、零坏行。"""
    n = 3
    p = corpus.write_corpus(tmp_path / "seq.jsonl",
                            [corpus.valid_lines(n), corpus.truncated_multibyte()])
    append_record(p, corpus.record(100))       # 内建 repair_tail
    recs = list(iter_records(p))
    assert [r.index for r in recs] == list(range(n + 1))
    assert recs[-1].obj == corpus.record(100)
    assert bad_line_stats.get(str(p), 0) == 0


def test_append_without_repair_glues_lines(tmp_path):
    """阳性对照(证夹具真能复现病):同一断尾文件不修尾直接追加 → 粘出坏行。"""
    n = 3
    p = corpus.write_corpus(tmp_path / "glue.jsonl",
                            [corpus.valid_lines(n), corpus.truncated_multibyte()])
    with open(p, "a", encoding="utf-8") as f:  # 本仓五处裸追加写的原样
        f.write(json.dumps(corpus.record(100), ensure_ascii=False) + "\n")
    recs = list(iter_records(p))
    assert len(recs) == n                      # 新记录被残字节吃掉,不是 n+1
    assert bad_line_stats[str(p)] == 1         # 粘成的那一行算坏行


def test_huge_truncated_tail_crosses_blocks(tmp_path):
    """超长断尾(>64KB 无换行):反向按块扫仍找到正确切点。"""
    head = corpus.valid_lines(2) + corpus.huge_valid_line()
    tail = corpus.huge_truncated_multibyte()
    assert len(tail) > 64 * 1024 and b"\n" not in tail
    p = corpus.write_corpus(tmp_path / "huge.jsonl", [head, tail])
    assert count_records(p) == 3

    assert repair_tail(p) == len(tail)
    assert p.read_bytes() == head
    assert count_records(p) == 3


def test_missing_and_empty_file(tmp_path):
    """文件不存在 / 空文件:iter 空、count 0、repair 0,均不抛。"""
    missing = tmp_path / "nope.jsonl"
    assert list(iter_records(missing)) == []
    assert count_records(missing) == 0
    assert repair_tail(missing) == 0
    assert not missing.exists()

    empty = corpus.write_corpus(tmp_path / "empty.jsonl", [b""])
    assert list(iter_records(empty)) == []
    assert count_records(empty) == 0
    assert repair_tail(empty) == 0
    assert bad_line_stats == {}


def test_blank_lines_are_silently_skipped(tmp_path, capsys):
    """空行/纯空白行:不进 stats、不告警、不占 index。"""
    p = corpus.write_corpus(tmp_path / "blank.jsonl", [
        corpus.valid_lines(1, start=0),
        corpus.blank_lines(),
        corpus.valid_lines(1, start=1),
    ])
    recs = list(iter_records(p))
    assert [r.index for r in recs] == [0, 1]
    assert [r.obj for r in recs] == corpus.valid_objs(2)
    assert capsys.readouterr().out == ""
    assert bad_line_stats == {}


def test_strict_raises_on_bad_line(tmp_path):
    """strict=True:坏行抛 ValueError;阳性对照:同文件 strict=False 正常跳过。"""
    p = corpus.write_corpus(tmp_path / "strict.jsonl", [
        corpus.valid_lines(1, start=0),
        corpus.non_dict_line(),
        corpus.valid_lines(1, start=1),
    ])
    with pytest.raises(ValueError):
        list(iter_records(p, strict=True))
    with pytest.raises(ValueError):
        count_records(p, strict=True)

    assert [r.obj for r in iter_records(p, strict=False)] == corpus.valid_objs(2)
    assert count_records(p, strict=False) == 2


def test_concurrent_write_between_reads_blocks_truncate(tmp_path):
    """双读稳定性:两次尾片快照之间文件变了(并发写者)→ 不截断、字节不变。"""
    p = corpus.write_corpus(tmp_path / "race.jsonl",
                            [corpus.valid_lines(2), corpus.truncated_multibyte()])
    injected = b"\xe6\xae\x8b more-bytes"
    real_fstat = os.fstat
    calls = {"n": 0}

    def racing_fstat(fd):
        calls["n"] += 1
        if calls["n"] == 2:                    # 首读之后、二次快照之前写者又追加了
            with open(p, "ab") as w:
                w.write(injected)
        return real_fstat(fd)

    before = p.read_bytes()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "fstat", racing_fstat)
        assert repair_tail(p) == 0
    assert p.read_bytes() == before + injected  # 一个字节都没截


def test_io_errors_propagate(tmp_path):
    """I/O 错误原样上抛:不得退化成空迭代 / 返回 0(要与「文件是空的」可区分)。"""
    p = corpus.write_corpus(tmp_path / "io.jsonl",
                            [corpus.valid_lines(1), corpus.truncated_multibyte()])
    real_open = builtins.open

    def blocked_open(file, *a, **kw):
        if str(file) == str(p):
            raise OSError(5, "输入/输出错误")
        return real_open(file, *a, **kw)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(builtins, "open", blocked_open)
        with pytest.raises(OSError):
            list(iter_records(p))
        with pytest.raises(OSError):
            count_records(p)

    def blocked_fstat(fd):
        raise OSError(5, "输入/输出错误")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "fstat", blocked_fstat)
        with pytest.raises(OSError):
            repair_tail(p)


def test_repair_is_idempotent(tmp_path):
    """幂等:修过再修返回 0,文件不再变。"""
    head = corpus.valid_lines(2)
    p = corpus.write_corpus(tmp_path / "idem.jsonl", [head, corpus.truncated_multibyte()])
    assert repair_tail(p) > 0
    after = p.read_bytes()
    assert repair_tail(p) == 0
    assert p.read_bytes() == after == head

    p2 = corpus.write_corpus(tmp_path / "idem2.jsonl", [corpus.complete_no_newline()])
    assert repair_tail(p2) == 0
    sealed = p2.read_bytes()
    assert repair_tail(p2) == 0
    assert p2.read_bytes() == sealed


def test_append_record_creates_file_and_uses_ensure_ascii_false(tmp_path):
    """append_record:新建文件、中日文不转义、不可序列化对象走 default。"""
    p = tmp_path / "new.jsonl"
    append_record(p, {"text": "日本語と中文", "obj": object()})
    raw = p.read_bytes().decode("utf-8")
    assert raw.endswith("\n") and "日本語と中文" in raw
    rec = json.loads(raw)
    assert isinstance(rec["obj"], str)
    assert count_records(p) == 1


def test_strict_is_explicit_param_only(tmp_path):
    """strict 只是显式参数,没有 config 旋钮(首审 M7:全局旋钮下四个生产读者
    对 ValueError 的既有处置互不相同——吞成 None/0/上抛,「口径唯一」当场破裂)。"""
    p = corpus.write_corpus(tmp_path / "cfg.jsonl", [corpus.non_dict_line()])
    assert count_records(p) == 0                # 缺省宽容
    assert not hasattr(jsonl, "_strict_default")
    import inspect
    assert inspect.signature(jsonl.iter_records).parameters["strict"].default is False


def test_pseudo_whitespace_only_line_is_bad_not_blank(tmp_path):
    """整行只有 U+2028/U+0085:是坏行不是空行(它们是记录内容不是行界,首审 L12)。
    阳性对照:ASCII 空白行仍静默跳过。"""
    reset_stats()
    p = corpus.write_corpus(tmp_path / "pseudo.jsonl", [
        corpus.valid_lines(1),
        " ".encode("utf-8") + b"\n",
        b"   \t\r\n",
        corpus.valid_lines(1, start=1),
    ])
    assert count_records(p) == 2
    assert bad_line_stats[str(p)] == 1          # 伪空白行计坏;ASCII 空白行不计


def test_not_a_directory_reads_as_missing(tmp_path):
    """路径中段是文件(NotADirectoryError):对读者与「不存在」同义,不上抛(首审 L14)。"""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    p = blocker / "sub" / "history.jsonl"
    assert list(iter_records(p)) == []
    assert count_records(p) == 0


def test_repair_lets_go_when_tail_changes_at_same_size(tmp_path, monkeypatch):
    """双读稳定性:大小相同但尾片内容变了(原地覆写)同样要让路(首审 L19)。
    阳性对照:干扰消失后同一文件正常截断。"""
    p = corpus.write_corpus(tmp_path / "race2.jsonl",
                            [corpus.valid_lines(1), corpus.truncated_multibyte()])
    size_before = p.stat().st_size
    real_fstat = os.fstat
    calls = {"n": 0}

    def racing_fstat(fd):
        calls["n"] += 1
        if calls["n"] == 2:                     # 第 2 次 fstat = 断尾路的 size2 快照
            data = bytearray(p.read_bytes())
            data[-1] ^= 0x01                    # 等长原地翻一位:size 不变,尾片变
            p.write_bytes(bytes(data))
        return real_fstat(fd)

    monkeypatch.setattr(os, "fstat", racing_fstat)
    assert repair_tail(p) == 0                  # 让路:不截断
    monkeypatch.undo()
    assert calls["n"] >= 2                      # 干扰真的打进了 size2 快照点
    assert p.stat().st_size == size_before      # 没有任何截断发生
    assert repair_tail(p) > 0                   # 阳性对照:无干扰时同一断尾被截掉


# ---- iter_records_reversed:文件尾反向扫描(B3 幂等键回查的依赖) ----

def test_reversed_yields_newest_first_and_stops_on_break(tmp_path):
    p = corpus.write_corpus(tmp_path / "tail.jsonl", [corpus.valid_lines(300)])
    got = []
    for obj in jsonl.iter_records_reversed(p):
        got.append(obj["index"])
        if len(got) == 5:
            break                               # 命中即停是本函数存在的理由
    assert got == [299, 298, 297, 296, 295]
    assert [o["index"] for o in jsonl.iter_records_reversed(p)][:3] == [299, 298, 297]


def test_reversed_skips_bad_lines_without_stats(tmp_path):
    reset_stats()
    p = corpus.write_corpus(tmp_path / "tail2.jsonl", [
        corpus.valid_lines(3), corpus.non_dict_line(),
        corpus.undecodable_line(), corpus.valid_lines(2, start=3),
        corpus.truncated_multibyte(),
    ])
    assert [o["index"] for o in jsonl.iter_records_reversed(p)] == [4, 3, 2, 1, 0]
    assert bad_line_stats == {}                 # 高频探询,不计坏行
    assert count_records(p) == 5 and bad_line_stats[str(p)] >= 1   # 阳性对照


def test_reversed_window_boundary(tmp_path):
    """窗口上限切在行中间 → 半行丢弃;恰落在行界 → 该行完整,照常产出(首审 L14)。

    ⚠️ 记录必须**逐字节等长**,否则「恰落行界」根本构造不出来——corpus.record 的 role
    按奇偶交错(user/assistant 差 5 字节),初版夹具用它,上限永远切在行中,该分支从没
    被测过(变异体存活揪出,2026-08-22)。"""
    lines = [corpus.encode({"idx": i, "pad": "桁桁桁等长正文"}) + b"\n" for i in range(3)]
    assert len({len(l) for l in lines}) == 1    # 等长前提自证,防夹具再犯
    per = len(lines[0])
    p = corpus.write_corpus(tmp_path / "tail3.jsonl", lines)
    out = [o["idx"] for o in jsonl.iter_records_reversed(p, max_bytes=2 * per)]
    assert out == [2, 1]                        # 上限恰落行界:第 1 行完整,照常产出
    out2 = [o["idx"] for o in jsonl.iter_records_reversed(p, max_bytes=2 * per + 3)]
    assert out2 == [2, 1]                       # 切中第 0 行中部:半行丢弃,不混残片


def test_reversed_crosses_blocks_and_handles_huge_lines(tmp_path):
    """>64KB 的记录跨反向块边界仍完整产出。"""
    p = corpus.write_corpus(tmp_path / "tail4.jsonl", [
        corpus.valid_lines(2), corpus.huge_valid_line(903), corpus.valid_lines(1, start=3)])
    out = [o["index"] for o in jsonl.iter_records_reversed(p)]
    assert out == [3, 903, 1, 0]


def test_reversed_missing_empty_and_zero_budget(tmp_path):
    assert list(jsonl.iter_records_reversed(tmp_path / "nope.jsonl")) == []
    p = tmp_path / "empty.jsonl"
    p.write_bytes(b"")
    assert list(jsonl.iter_records_reversed(p)) == []
    q = corpus.write_corpus(tmp_path / "z.jsonl", [corpus.valid_lines(2)])
    assert list(jsonl.iter_records_reversed(q, max_bytes=0)) == []   # 0 预算=空,不是全量
