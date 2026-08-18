"""`scripts/doc_numbers.py` 金测试（波 A3；2026-08-18 换宿主后重写）。

打点宿主是 **`EXECUTION_LEDGER.md` §2 的「当前规模」表**，不是 `CHARTER.md`。
用户拍板把精确数字整个挪出宪章、只留指向台账的指针：宪章是每个新会话的第一读物，
而它每改一次都要用户显式同意——把易变的数字放在那里，等于造一道天天要拍板的门。
宿主可写之后 `--write` 一条命令即可刷新。

守四件，全是「判据不可能失败」的反面：
  ★ **锚点匹配不到 ≠ 无漂移**。表被改写成正则命中 0 次（或命中多次）时必须记
    rc=1「跑不起来」——记 0 就是这个守卫自己悄悄关掉了自己，而且从此永远绿。
  ★ **金测试项数取不到时输出 unknown，绝不回落到上次的数**（rc=1）。回落只是把一个
    假绿换成另一个假绿；`--write` 更不许把 unknown 顺手写成一个假事实。
  ★ **`--write` 只改数字，不动口径说明**。口径列是人写的散文，被机器覆盖掉就等于把
    「这个数怎么来的」这条信息弄丢了。
  ★ **`CHARTER.md` 永不被自动改写**，且换宿主后连打点面都不再是。

脚手架：把脚本拷进 tmp_path 的 `scripts/` 下（脚本用 `__file__` 的父目录的父目录当
仓库根），再造一个最小假仓库。真台账/真宪章/真 config 一律不碰。
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC = os.path.join(_REPO, "scripts", "doc_numbers.py")

# 各行的值与下面 _fake_repo 造出来的实际内容一一对应
_LEDGER_OK = """\
## §2 总进度表

### 当前规模（`./devctl.sh doc-numbers` 派生，**勿手改**）

| 项 | 当前值 | 口径 |
|---|---|---|
| 金测试项数 | 137 | `pytest --collect-only`（含 parametrize 展开） |
| 测试文件数 | 4 | `backend/tests/` 下递归的 `test_*.py` |
| mcp 文件数 | 2 | 递归 `.py`，**不含** `__init__.py` |
| BaseTool 子类 | 3 | `class X(BaseTool)` 的声明数 |
| 物理隔离沙箱 | 1 | `agents/*_sandbox.py` |
| 剧本 | 0 | 仓库根 `skills/` 下的 `.md` |
| 协议夹具 | 0 | `protocol/fixtures/` 下的 `*.events.jsonl` |
| model_registry 档位 | 2 | `config.yaml` 的条目数 |
| rag.tiers 档位 | 1 | `config.yaml` 的档位数 |
"""

_CONFIG = """\
inference:
  model_registry:
    - repo: "a"
    - repo: "b"
rag:
  tiers:
    - id: "t1"
"""


def _fake_repo(tmp_path, ledger=_LEDGER_OK, charter="# 假宪章\n只留指针，不放数字。\n"):
    (tmp_path / "scripts").mkdir()
    shutil.copy(_SRC, tmp_path / "scripts" / "doc_numbers.py")
    (tmp_path / "EXECUTION_LEDGER.md").write_text(ledger, encoding="utf-8")
    (tmp_path / "CHARTER.md").write_text(charter, encoding="utf-8")
    (tmp_path / "config.yaml").write_text(_CONFIG, encoding="utf-8")

    tests = tmp_path / "backend" / "tests"
    tests.mkdir(parents=True)
    for i in range(4):
        (tests / f"test_{i}.py").write_text("def test_x(): pass\n", encoding="utf-8")

    mcp = tmp_path / "backend" / "framework" / "mcp" / "tools"
    mcp.mkdir(parents=True)
    (mcp / "__init__.py").write_text("", encoding="utf-8")          # 不计入 mcp_files
    (mcp / "a.py").write_text("class Foo(BaseTool):\n    pass\n", encoding="utf-8")
    (mcp / "b.py").write_text("class Bar(BaseTool):\n    pass\n"
                              "class Baz(BaseTool):\n    pass\n", encoding="utf-8")

    agents = tmp_path / "backend" / "framework" / "agents"
    agents.mkdir(parents=True)
    (agents / "math_sandbox.py").write_text("", encoding="utf-8")

    (tmp_path / "skills").mkdir()
    (tmp_path / "protocol" / "fixtures").mkdir(parents=True)

    # 三处「第三方代码」诱饵，各自只被一条排除规则挡住（规则间刻意不重叠，
    # 否则拆掉任一条另一条还在，变异体就杀不死）
    for rel, marker in (("backend/.venv-mlxsrc", False),          # 只靠「.venv 前缀」
                        ("backend/vendor/site-packages", False),  # 只靠「site-packages」
                        ("backend/thirdparty", True)):            # 只靠 pyvenv.cfg 标记
        d = tmp_path / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "evil.py").write_text("class Evil(BaseTool):\n    pass\n", encoding="utf-8")
        if marker:
            (d / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")

    return tmp_path / "scripts" / "doc_numbers.py"


def _fake_venv(tmp_path, *, count=137, rc=0, stdout=None):
    """假 backend/.venv/bin/python，冒充 pytest --collect-only 的输出。

    `rc` 与 `stdout` 分别可给：**收集期报错时 pytest 会「既打出计数又非零退出」**，
    这个组合是唯一能杀死 returncode 守卫的形态。
    """
    vbin = tmp_path / "backend" / ".venv" / "bin"
    vbin.mkdir(parents=True)
    py = vbin / "python"
    if stdout is None:
        stdout = f"{count} tests collected in 1.0s"
    body = "#!/bin/sh\n" + (f"echo '{stdout}'\n" if stdout else "") + f"exit {rc}\n"
    py.write_text(body, encoding="utf-8")
    py.chmod(0o755)
    return py


def _run(script, *args):
    return subprocess.run([sys.executable, str(script), *args],
                          capture_output=True, text=True, timeout=300)


def _out(r):
    return r.stdout + r.stderr


def _row(tmp_path, label):
    text = (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8")
    m = re.search(rf"^\| {re.escape(label)} \| (\d+) \| (.*)$", text, re.M)
    assert m, f"台账里找不到 {label} 行"
    return int(m.group(1)), m.group(2)


# ---- 一致 / 漂移 ----

def test_no_drift_is_rc0(tmp_path):
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=137)
    r = _run(script, "--check")
    assert r.returncode == 0, _out(r)
    assert "全部一致" in r.stdout


@pytest.mark.parametrize("label", [
    "金测试项数", "测试文件数", "mcp 文件数", "BaseTool 子类", "物理隔离沙箱",
    "剧本", "协议夹具", "model_registry 档位", "rag.tiers 档位",
])
def test_every_anchored_row_is_actually_checked(tmp_path, label):
    """★ 九行每一行都得真被校验。只测一两行的话，某行的锚点悄悄失配也没人知道。"""
    ledger = re.sub(rf"^\| {re.escape(label)} \| \d+ \|",
                    f"| {label} | 77 |", _LEDGER_OK, count=1, flags=re.M)
    assert ledger != _LEDGER_OK, label            # 夹具本身确实改动了那一行
    script = _fake_repo(tmp_path, ledger=ledger)
    _fake_venv(tmp_path, count=137)
    r = _run(script, "--check")
    assert r.returncode == 3, _out(r)
    assert label in r.stdout


def test_growth_is_reported_as_drift(tmp_path):
    """加了测试就该报漂移——精确值不再用档位近似（宿主可写后不必再迁就宪章）。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=138)
    r = _run(script, "--check")
    assert r.returncode == 3, _out(r)
    assert "138" in r.stdout


# ---- ★ 锚点失配 ≠ 无漂移 ----

def test_anchor_matching_nothing_is_internal_error_not_pass(tmp_path):
    """★ 最要紧的一条：表被改写成正则匹配不到的样子时，「没找到要比的东西」绝不能
    被当成「没有漂移」——那样这个守卫从此永远绿。"""
    ledger = _LEDGER_OK.replace("| mcp 文件数 | 2 |", "| mcp 文件数（约）| 2 |")
    script = _fake_repo(tmp_path, ledger=ledger)
    _fake_venv(tmp_path)
    r = _run(script, "--check")
    assert r.returncode == 1, _out(r)
    assert "锚点命中 0 次" in r.stdout


def test_anchor_matching_twice_is_internal_error(tmp_path):
    """命中多次 = 口径有歧义，同样不许当成「已核对」。"""
    script = _fake_repo(tmp_path, ledger=_LEDGER_OK + "\n| 剧本 | 0 | 抄了一遍 |\n")
    _fake_venv(tmp_path)
    r = _run(script, "--check")
    assert r.returncode == 1, _out(r)
    assert "锚点命中 2 次" in r.stdout


def test_internal_error_outranks_drift(tmp_path):
    """既有漂移又有锚点失配时报 1：这份校验整体不可信，比「有条数字旧了」更该管。"""
    ledger = (_LEDGER_OK
              .replace("| mcp 文件数 | 2 |", "| mcp 文件数（约）| 2 |")
              .replace("| 剧本 | 0 |", "| 剧本 | 9 |"))
    script = _fake_repo(tmp_path, ledger=ledger)
    _fake_venv(tmp_path)
    r = _run(script, "--check")
    assert r.returncode == 1, _out(r)


def test_missing_ledger_is_internal_error(tmp_path):
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path)
    (tmp_path / "EXECUTION_LEDGER.md").unlink()
    r = _run(script, "--check")
    assert r.returncode == 1, _out(r)
    assert "文件不存在" in r.stdout


# ---- ★ 乙类取不到真值时 unknown，绝不回落 ----

def test_missing_venv_yields_unknown_not_a_stale_number(tmp_path):
    script = _fake_repo(tmp_path)                 # 刻意不建 venv
    r = _run(script, "--check")
    assert r.returncode == 1, _out(r)
    assert "unknown" in r.stdout
    assert "派生不出真值" in r.stdout


def test_broken_collector_yields_unknown(tmp_path):
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, rc=4, stdout="")
    r = _run(script, "--check")
    assert r.returncode == 1, _out(r)
    assert "unknown" in r.stdout


def test_partial_collection_is_unknown_not_a_smaller_truth(tmp_path):
    """★ 收集期报错时 pytest **既打出计数又非零退出**，而那个计数是残缺收集的结果。
    把它当真值 = 拿一个偏小的数去当权威，比 unknown 更坏（unknown 至少会响）。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, rc=2, stdout="137 tests collected, 1 error in 0.05s")
    r = _run(script, "--check")
    assert r.returncode == 1, _out(r)
    assert "收集不完整" in r.stdout


# ---- ★ --write：只改数字，不动口径 ----

def test_write_fixes_drift_and_leaves_the_caliber_column_alone(tmp_path):
    """★ 口径列是人写的散文——被机器覆盖掉就等于把「这个数怎么来的」弄丢了。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=1298)
    before_value, before_caliber = _row(tmp_path, "金测试项数")
    assert before_value == 137

    w = _run(script, "--write")
    assert w.returncode == 3, _out(w)             # 报告的是**改写前**的判决
    after_value, after_caliber = _row(tmp_path, "金测试项数")
    assert after_value == 1298
    assert after_caliber == before_caliber        # 口径列一字未动

    # ★ 改写自己说改成功了不算数——复跑才算
    assert _run(script, "--check").returncode == 0


def test_write_leaves_everything_except_the_drifted_numbers_byte_identical(tmp_path):
    """★ 上一条只查了一行的口径列。真正要守的是：**除了那几个漂移的数字，整份文件
    逐字节不变**——小节标题、引言散文、其余八行的口径列都不许被机器碰（首审 #26）。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=1298)
    before = (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8")
    assert _run(script, "--write").returncode == 3
    after = (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8")
    # 唯一允许的差异：金测试项数那一行的数字
    assert after == before.replace("| 金测试项数 | 137 |", "| 金测试项数 | 1298 |")


def test_write_refuses_when_the_text_moved_under_it(tmp_path):
    """★ `rewrite` 收了「校验时看到的旧值」却从不核对，那道保险等于不存在（首审 #12）。
    这里模拟「校验之后、改写之前文件被人动了」：改写必须整体放弃，一个字都不写。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=1298)
    # 让脚本先算出 drift_specs（旧值 137），再在改写前把文件换掉
    import shutil as _sh
    _sh.copy(_SRC, tmp_path / "scripts" / "doc_numbers.py")
    ledger = tmp_path / "EXECUTION_LEDGER.md"
    ledger.write_text(_LEDGER_OK.replace("| 金测试项数 | 137 |",
                                         "| 金测试项数 | 137 |"), encoding="utf-8")
    # 直接单测 rewrite 本身：给一个与文件现状不符的 old
    import importlib.util
    spec = importlib.util.spec_from_file_location("dn_rw", tmp_path / "scripts" / "doc_numbers.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    before = ledger.read_text(encoding="utf-8")
    got = m.rewrite(ledger, [(r"^\| 金测试项数 \| (\d+) \|", "999", "1298")])
    assert got is None
    assert ledger.read_text(encoding="utf-8") == before


def test_write_tells_you_to_recheck(tmp_path):
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=999)
    assert "复跑" in _run(script, "--write").stdout


def test_write_does_nothing_when_there_is_no_drift(tmp_path):
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=137)
    before = (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8")
    r = _run(script, "--write")
    assert r.returncode == 0, _out(r)
    assert (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8") == before


def test_write_refuses_when_the_value_cannot_be_derived(tmp_path):
    """★ 派生不出真值时绝不能「顺手写个数进去」——那是把 unknown 固化成假事实。"""
    script = _fake_repo(tmp_path)                 # 无 venv → 金测试项数 unknown
    before = (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8")
    r = _run(script, "--write")
    assert r.returncode == 1, _out(r)
    assert (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8") == before


def test_write_touches_nothing_when_any_row_is_untrustworthy(tmp_path):
    """★ 上一条只覆盖了「没有别的漂移」的情形，是半个判据。真正要守的是：**只要有
    一项跑不起来，就一行都不许改**——在一份读不准的图纸上照着改，比不改更坏。

    这里同时制造「金测试项数派生不出」与「剧本行确有漂移」，后者本来是可写的。
    """
    ledger = _LEDGER_OK.replace("| 剧本 | 0 |", "| 剧本 | 9 |")
    script = _fake_repo(tmp_path, ledger=ledger)   # 仍不建 venv → golden 未知
    before = (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8")

    r = _run(script, "--write")
    assert r.returncode == 1, _out(r)
    assert "不改写任何文件" in r.stdout
    assert (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8") == before


# ---- 红线：宪章既不打点也不改写 ----

@pytest.mark.parametrize("argv", [[], ["--check"], ["--write"]])
def test_charter_is_never_touched(tmp_path, argv):
    """宪章的任何改动都要用户显式同意。

    ⚠️ **如实标注**：换宿主之后，没有任何代码路径会打开 `CHARTER.md` 写——没有锚点
    指向它，`rewrite()` 也只在 `writable` 里的文件上调用。所以「文件未被改动」这半条
    今天**不可证伪**。保留它是当**回归网**：将来若有人把锚点又指回宪章，或让
    `NEVER_WRITE` 失效，这条会第一时间挡住。
    （首审 #8：上一版把这条标了 ★ 却删掉了原有的诚实说明，还顺手丢弃了返回值——
    脚本崩出 traceback 它照样绿。返回值现在断言。）
    """
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=999)               # 制造漂移，走到会改写的那条路
    before = (tmp_path / "CHARTER.md").read_text(encoding="utf-8")
    r = _run(script, *argv)
    assert r.returncode == 3, _out(r)             # 真走到了「有漂移」那条路
    assert "Traceback" not in _out(r), _out(r)
    assert (tmp_path / "CHARTER.md").read_text(encoding="utf-8") == before


def test_charter_is_not_an_anchor_host():
    """把数字挪出宪章之后，它不该再出现在任何锚点里——否则挪的只是一半。

    直接读 `anchors()` 的 file 字段，而不是「输出里没出现 CHARTER.md」——后者在
    绿跑时恒真（绿跑本来就不打文件名），只是碰巧能抓住变异体（首审 #25）。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("dn_anchors", _SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    hosts = {a["file"] for a in m.anchors({k: 1 for k in
             ("golden_tests", "test_files", "mcp_files", "tool_classes", "sandboxes",
              "skills", "fixtures", "model_registry", "rag_tiers")}, 1)}
    assert hosts == {"EXECUTION_LEDGER.md"}, hosts
    assert m.NEVER_WRITE == {"CHARTER.md"}


# ---- 甲类口径 ----

def test_text_quantities_use_the_stated_calibers(tmp_path):
    """__init__.py 不计入 mcp_files；BaseTool 子类按声明数计（一文件可多个）。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path)
    out = _run(script, "--check").stdout
    for frag in ("mcp_files        2", "tool_classes     3", "sandboxes        1",
                 "test_files       4", "model_registry   2", "rag_tiers        1"):
        assert frag in out, out


def test_third_party_virtualenvs_are_never_counted(tmp_path):
    """★ 假仓库里埋了三处第三方 `class X(BaseTool)`（.venv-mlxsrc / vendor/site-packages
    / 带 pyvenv.cfg 的普通名目录），每处只被一条排除规则挡住。tool_classes 必须仍是
    本仓那 3 个——按精确目录名 `.venv` 排除是不够的。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path)
    assert "tool_classes     3" in _run(script, "--check").stdout


def test_every_derived_value_is_now_consumed_by_an_anchor(tmp_path):
    """派生 ≠ 校验。换宿主后九项**全部**都有锚点在消费，故应全部带 ✓——
    此前只有四项被消费，而输出的口吻像是都核对过。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path)
    keys = ("test_files", "mcp_files", "tool_classes", "sandboxes", "skills",
            "fixtures", "model_registry", "rag_tiers", "golden_tests")
    lines = [ln for ln in _run(script, "--check").stdout.splitlines()
             if any(k in ln for k in keys)]
    assert len(lines) == 9, lines
    assert all(ln.strip().startswith("✓") for ln in lines), lines


def test_check_and_write_are_mutually_exclusive(tmp_path):
    """★ `--check` 此前被解析却从不读，于是 `--check --write` 会照样改写治理文档，
    而用户以为自己要的是只读校验（首审 #5）。互斥，且**只读的一方赢**。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=999)                  # 有漂移，走得到改写那条路
    before = (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8")
    r = _run(script, "--check", "--write")
    assert r.returncode == 2, _out(r)
    assert (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8") == before


def test_anchor_is_bound_to_the_scale_section(tmp_path):
    """★ 锚点若不绑定小节，「本表被删、别处恰好有张同名行的表」会让 `--check` 与
    `--write` **静默改指错的行**——重复能被「命中 != 1」拦下，这个不能（首审 #18）。

    这里把「当前规模」整节删掉，另起一张风马牛不相及的表，行名恰好撞上。
    """
    decoy = """\
## §9 别的什么表

| 项 | 当前值 | 口径 |
|---|---|---|
| 金测试项数 | 137 | 这是另一张表里的同名行，不该被当成锚点 |
| 测试文件数 | 4 | 同上 |
| mcp 文件数 | 2 | 同上 |
| BaseTool 子类 | 3 | 同上 |
| 物理隔离沙箱 | 1 | 同上 |
| 剧本 | 0 | 同上 |
| 协议夹具 | 0 | 同上 |
| model_registry 档位 | 2 | 同上 |
| rag.tiers 档位 | 1 | 同上 |
"""
    script = _fake_repo(tmp_path, ledger=decoy)      # 刻意没有「### 当前规模」小节
    _fake_venv(tmp_path, count=137)
    before = (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8")
    r = _run(script, "--check")
    # 小节不在 = 打点面没了 = 跑不起来；**绝不能**因为别处行对得上就报「一致」。
    # ⚠️ 只断言 rc==1 是不够的：Python 崩出 traceback 的退出码**也是 1**，那样
    # 「守卫生效」与「脚本自己炸了」分不开（实测：把守卫改成 if False 会崩，而
    # 这条曾因此判不出变异体）。故连消息与「没有 traceback」一起断言。
    assert r.returncode == 1, _out(r)
    assert "Traceback" not in _out(r), _out(r)
    assert "找不到「### 当前规模」小节" in r.stdout, _out(r)
    assert "全部一致" not in r.stdout
    assert (tmp_path / "EXECUTION_LEDGER.md").read_text(encoding="utf-8") == before
