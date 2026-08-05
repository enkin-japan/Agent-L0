"""Excel 底盘基准题库金测试（OfficeCLI 集成第 3 站）：鉴定纪律与裁判独立性。

守的是什么：
① **裁判不下场**——expected 全部是手写字面量，绝不经任何引擎回填（l0_eval 同款
  纪律：评测框架的判分基准独立于被评测者）。
② **输入形态=生产同形**——build_workbook 用 openpyxl 写裸公式（无缓存值），这是
  verify 链真实吃到的文件形态；换成 officecli 写入会把"裸新函数名 #NAME?"这一类
  真实形态差异测没。
③ **题库结构**——id 唯一、公式以 = 开头、错误题与链式依赖题在位（判别性核心）。
"""
import re
from pathlib import Path

RUNNER = (Path(__file__).resolve().parents[1] / "benchmarks" / "officecli_eval"
          / "run_officecli_eval.py")


def _load_cases():
    import importlib.util
    spec = importlib.util.spec_from_file_location("oc_eval", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bank_structure_and_coverage():
    m = _load_cases()
    ids = [c[0] for c in m.CASES]
    assert len(ids) == len(set(ids)), "题 id 重复"
    assert len(m.CASES) >= 30
    for cid, formula, _exp in m.CASES:
        assert formula.startswith("="), f"{cid} 公式须以 = 开头"
    # 判别性核心必须在位：错误传播 + 链式依赖 + 跨表 + 查找族
    assert sum(1 for c in m.CASES if c[2] is m.ERROR) >= 2
    assert any("E29" in c[1] for c in m.CASES), "链式依赖题缺失"
    assert any("Data!" in c[1] for c in m.CASES), "跨表题缺失"
    assert any("VLOOKUP" in c[1] or "XLOOKUP" in c[1] for c in m.CASES)


def test_judge_independence_and_production_shape():
    src = RUNNER.read_text(encoding="utf-8")
    # 裁判不下场：expected 是字面量元组，源码里不得有"从引擎读回填 expected"的路径
    assert "expected" not in src.split("CASES = [")[1].split("]")[0].lower()
    # 生产同形：题库工件必须 openpyxl 写（裸公式无缓存值），不得经 officecli 写入
    assert "from openpyxl import Workbook" in src
    build = src.split("def build_workbook")[1].split("def ")[0]
    assert "officecli" not in build and "run_cli" not in build
