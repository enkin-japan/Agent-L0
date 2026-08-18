"""`scripts/doc_numbers.py` 金测试（波 A3）。

守三件事，全是「判据不可能失败」的反面：
  ★ **锚点匹配不到 ≠ 无漂移**。文档被改写成正则命中 0 次（或命中多次）时必须记
    rc=1「跑不起来」——记 0 就是这个守卫自己悄悄关掉了自己，而且从此永远绿。
  ★ **金测试项数取不到时输出 unknown，绝不回落到上次的数**（rc=1）。回落只是把一个
    假绿换成另一个假绿。
  ★ **档位下限恒 ≤ 实际值**。方向反了（声称的下限高于实际）就是一句假声明，
    而档位机制的全部意义就是「这句话不会因为数字长大而变假」。
另守一条红线：**CHARTER.md 永不被自动改写**（宪章的任何改动都要用户显式同意）。

脚手架：把脚本拷进 tmp_path 的 `scripts/` 下（脚本用 `__file__` 的父目录的父目录当
仓库根），再造一个最小假仓库。真 CHARTER/真 config 一律不碰。
"""
import os
import shutil
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC = os.path.join(_REPO, "scripts", "doc_numbers.py")

# 派生值与下面 _fake_repo 造出来的实际内容一一对应
_CHARTER_OK = """\
# 假宪章
        │ 工具   mcp/（2 文件 3 类）+ agents/（1 沙箱，物理隔离）               │
2. 后端改动 → 跑 `backend/tests/`（100+ 项金测试，FakeEngine 无模型毫秒级）
| 回归网 | `backend/tests/`（**100+** 金测试） | x | y |
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


def _fake_repo(tmp_path, charter=_CHARTER_OK):
    (tmp_path / "scripts").mkdir()
    shutil.copy(_SRC, tmp_path / "scripts" / "doc_numbers.py")
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

    # ★ 三处「第三方代码」诱饵，各自模拟一种真实存在的形态。它们里的
    # `class X(BaseTool)` **一个都不许被算进 tool_classes**——算进去就意味着一次与本仓
    # 无关的 pip install 能静默改掉宪章里的数字。
    #   .venv          精确名（最早就排除了）
    #   .venv-mlxsrc   本仓真实存在的另一个环境，6206 个 .py；按精确名排除时它整个漏网
    #   thirdparty/    名字完全不像环境，靠 pyvenv.cfg 标记才认得出来
    # 每个诱饵刻意只被**一条**规则挡住，规则之间不重叠——否则拆掉任一条规则，
    # 诱饵仍被另一条挡着，变异体就杀不死（第一版三个诱饵各有两条保护，正是这样）。
    for rel, marker in (("backend/.venv-mlxsrc", False),      # 只靠「.venv 前缀」
                        ("backend/vendor/site-packages", False),  # 只靠「site-packages」
                        ("backend/thirdparty", True)):        # 只靠 pyvenv.cfg 标记
        d = tmp_path / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "evil.py").write_text("class Evil(BaseTool):\n    pass\n", encoding="utf-8")
        if marker:
            (d / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")

    return tmp_path / "scripts" / "doc_numbers.py"


def _fake_venv(tmp_path, *, count=137, rc=0, stdout=None):
    """造一个假 backend/.venv/bin/python，冒充 pytest --collect-only 的输出。

    `rc` 与 `stdout` 可分别给：**收集期报错时 pytest 会「既打出计数又非零退出」**，
    这个组合是唯一能杀死 returncode 守卫的形态（首审 #3：原来的 broken 桩是
    `exit 4` 且无输出，那样 `not m` 分支自己就接住了，守卫删掉照样全绿）。
    """
    vbin = tmp_path / "backend" / ".venv" / "bin"
    vbin.mkdir(parents=True)
    py = vbin / "python"
    if stdout is None:
        stdout = f"{count} tests collected in 1.0s"
    body = "#!/bin/sh\n"
    if stdout:
        body += f"echo '{stdout}'\n"
    body += f"exit {rc}\n"
    py.write_text(body, encoding="utf-8")
    py.chmod(0o755)
    return py


def _run(script, *args):
    return subprocess.run([sys.executable, str(script), *args],
                          capture_output=True, text=True, timeout=300)


def _out(r):
    return r.stdout + r.stderr


# ---- 一致 / 漂移 ----

def test_no_drift_is_rc0(tmp_path):
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=137)          # 137 → 档位 100 → 文档写的 100+ 正确
    r = _run(script, "--check")
    assert r.returncode == 0, _out(r)
    assert "全部一致" in r.stdout


def test_drift_in_counts_is_rc3(tmp_path):
    charter = _CHARTER_OK.replace("mcp/（2 文件 3 类）", "mcp/（9 文件 9 类）")
    script = _fake_repo(tmp_path, charter=charter)
    _fake_venv(tmp_path, count=137)
    r = _run(script, "--check")
    assert r.returncode == 3, _out(r)
    assert "漂移" in r.stdout


def test_crossing_a_band_is_drift(tmp_path):
    """测试数从 137 涨到 1138 时，文档的 100+ 必须被判为过期（应写 1000+）。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=1138)
    r = _run(script, "--check")
    assert r.returncode == 3, _out(r)
    assert "1000+" in r.stdout


def test_growth_inside_a_band_is_not_drift(tmp_path):
    """档位机制的意义：加了测试但没跨档，文档不必改（否则宪章要天天改）。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=199)
    r = _run(script, "--check")
    assert r.returncode == 0, _out(r)


# ---- ★ 锚点失配 ≠ 无漂移 ----

def test_anchor_matching_nothing_is_internal_error_not_pass(tmp_path):
    """★ 最要紧的一条：文档被改写成正则匹配不到的样子时，「没找到要比的东西」
    绝不能被当成「没有漂移」——那样这个守卫从此永远绿。"""
    charter = _CHARTER_OK.replace("mcp/（2 文件 3 类）", "工具若干、沙箱若干")
    script = _fake_repo(tmp_path, charter=charter)
    _fake_venv(tmp_path, count=137)
    r = _run(script, "--check")
    assert r.returncode == 1, _out(r)
    assert "锚点命中 0 次" in r.stdout


def test_anchor_matching_twice_is_internal_error(tmp_path):
    """命中多次 = 口径有歧义，同样不许当成「已核对」。"""
    charter = _CHARTER_OK + "\n再抄一遍：mcp/（2 文件 3 类）+ agents/（1 沙箱，物理隔离）\n"
    script = _fake_repo(tmp_path, charter=charter)
    _fake_venv(tmp_path, count=137)
    r = _run(script, "--check")
    assert r.returncode == 1, _out(r)
    assert "锚点命中 2 次" in r.stdout


def test_internal_error_outranks_drift(tmp_path):
    """既有漂移又有锚点失配时报 1：这份校验整体不可信，比「有条数字旧了」更该管。"""
    charter = (_CHARTER_OK
               .replace("mcp/（2 文件 3 类）", "写坏了")
               .replace("（100+ 项金测试", "（500+ 项金测试"))
    script = _fake_repo(tmp_path, charter=charter)
    _fake_venv(tmp_path, count=137)
    r = _run(script, "--check")
    assert r.returncode == 1, _out(r)


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
    把它当真值 = 拿一个偏小的数去当权威，比 unknown 更坏（unknown 至少会响）。
    这个「有输出 + 非零码」的组合是唯一能杀死 returncode 守卫的形态。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, rc=2, stdout="137 tests collected, 1 error in 0.05s")
    r = _run(script, "--check")
    assert r.returncode == 1, _out(r)
    assert "收集不完整" in r.stdout


def test_missing_anchor_file_is_internal_error(tmp_path):
    """锚点所在的文件整个不见了，同样不许当成「没有漂移」。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path)
    (tmp_path / "CHARTER.md").unlink()
    r = _run(script, "--check")
    assert r.returncode == 1, _out(r)
    assert "文件不存在" in r.stdout


# ---- ★ 档位下限恒 ≤ 实际值 ----

@pytest.fixture(scope="module")
def dn():
    """直接导入真脚本取纯函数——band_floor 不碰仓库，没必要为它建 12 次假仓库。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dn_mod", _SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.mark.parametrize("n", [0, 1, 37, 99, 100, 137, 199, 200, 999, 1000, 1138, 99999])
def test_band_floor_never_claims_more_than_reality(dn, n):
    """方向反了（声称的下限高于实际）就是一句假声明。初值写成 BANDS[0] 会在
    n<100 时正好犯这个错。"""
    assert dn.band_floor(n) <= n


@pytest.mark.parametrize("n,want", [(1163, 1000), (999, 500), (200, 200), (99, 0)])
def test_band_floor_picks_the_right_band(dn, n, want):
    """只钉 `≤ n` 的话，`return 0` 这个变异体能活下来（它恒满足不等式）。
    补一组精确值，让「取的是哪一档」这件事在本条里就说得清（首审 #15）。"""
    assert dn.band_floor(n) == want


# ---- 红线：宪章永不自动改写 ----

@pytest.mark.parametrize("argv", [[], ["--check"]])
def test_charter_is_never_rewritten(tmp_path, argv):
    """宪章红线：任何调用形态都不许改动 CHARTER.md，有漂移时也只打 diff。

    ⚠️ 如实标注（首审 #8）：`--write` 删掉后，脚本已**没有任何写文件的代码路径**，
    所以「文件未被改动」这半条今天是不可证伪的，真正有判别力的只有那句提示文案。
    保留它是当**未来的回归网**——将来加 `--write` 时，这条会第一时间挡住「顺手也把
    CHARTER 写了」。
    """
    charter = _CHARTER_OK.replace("mcp/（2 文件 3 类）", "mcp/（9 文件 9 类）")
    script = _fake_repo(tmp_path, charter=charter)
    _fake_venv(tmp_path, count=137)
    before = (tmp_path / "CHARTER.md").read_text(encoding="utf-8")

    r = _run(script, *argv)
    assert r.returncode == 3, _out(r)                       # 确实看见了漂移
    assert (tmp_path / "CHARTER.md").read_text(encoding="utf-8") == before
    assert "永不自动改写" in r.stdout


def test_no_write_flag_is_advertised(tmp_path):
    """没有可写的锚点时就不该有 --write：宣传一个不干活的旗标是同一个病。"""
    script = _fake_repo(tmp_path)
    r = _run(script, "--help")
    assert "--write" not in r.stdout, r.stdout


# ---- 甲类口径 ----

def test_text_quantities_use_the_stated_calibers(tmp_path):
    """__init__.py 不计入 mcp_files；BaseTool 子类按声明数计（一文件可多个）。"""
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path, count=137)
    out = _run(script, "--check").stdout
    assert "mcp_files        2" in out          # a.py + b.py，__init__.py 不算
    assert "tool_classes     3" in out          # Foo + Bar + Baz
    assert "sandboxes        1" in out
    assert "test_files       4" in out
    assert "model_registry   2" in out
    assert "rag_tiers        1" in out


def test_third_party_virtualenvs_are_never_counted(tmp_path):
    """★ 假仓库里埋了三处第三方 `class X(BaseTool)`（.venv / .venv-mlxsrc /
    带 pyvenv.cfg 的普通名目录）。tool_classes 必须仍是本仓那 3 个。

    按**精确目录名** `.venv` 排除时，本仓真实存在的 `backend/.venv-mlxsrc`
    （6206 个 .py）整个漏网，而当时的注释还在替这个洞打包票（首审 Critical 2）。
    """
    script = _fake_repo(tmp_path)
    _fake_venv(tmp_path)
    out = _run(script, "--check").stdout
    assert "tool_classes     3" in out, out
