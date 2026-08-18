#!/usr/bin/env python3
"""文档里的规模数字改为从仓库派生（波 A3 ≡ OSS 扫描 §3 A3）。

病根：`CHARTER.md` 是每个新会话的第一读物，而它第一页的规模判断是错的——写「600+
金测试」「mcp/ 27 文件 55 类」，而 2026-08-18 实测是 **1163 项 / 31 文件 / 67 类**
（低估约一倍）。散文里的数字没有任何机制会去更新它，只会越放越旧。

**口径分两类，这是本设计的要点**（首审订正：不分类的话，这条会把漂移从一处搬到
另一处）：

  甲类｜纯文本可派生、口径唯一
      测试文件数 / mcp 文件数 / BaseTool 子类数 / 沙箱数 / 剧本数 / 夹具数 /
      model_registry 与 rag.tiers 档位数。纯 stdlib，不 import backend。

  乙类｜必须跑收集器才有真值
      **金测试项数**。`grep '^def test_'` 与 pytest 的收集口径**互相排斥**——后者含
      parametrize 展开，前者永远数不出来。故此项只认
      `backend/.venv/bin/python -m pytest --collect-only -q`；
      **venv 不在就输出 unknown，绝不回落到上次的数**。回落即是把一个假绿换成另一个
      假绿——正是本脚本要消灭的那类失效。

金测试项数在文档里刻意用**档位下限**形式（`1000+`）而非精确值：精确值每加一条测试
就过期，而 `CHARTER.md` 按宪章纪律每次修改都要用户同意——那会造出一个天天红、
最终必被删掉的门。档位取「不超过实测值的最大整档」（100/200/500/1000/2000/5000…），
跨档才需要改文档。

退出码（与 `devctl check` 同一套三值纪律）：
    0 = 全部一致 ／ 3 = 有漂移 ／ 2 = 用法错 ／ 1 = 跑不起来（派生不出或锚点失配）

**锚点失配一律记 1，绝不记 0**：文档被改写成正则匹配不到的样子时，「没找到要比的
东西」不等于「没有漂移」——那是本脚本自己悄悄关掉自己。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

RC_PASS, RC_INTERNAL, RC_USAGE, RC_DRIFT = 0, 1, 2, 3

# 宪章的任何改动都要用户显式同意（CLAUDE.md 红线），故本脚本**永不**写任何文档，
# 尤其是它。也不提供「强制写」的旗标——给了就迟早会被用掉。
NEVER_WRITE = {"CHARTER.md"}

BANDS = (100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000)


def band_floor(n: int) -> int:
    """不超过 n 的最大整档。1138 → 1000；跨档才需要改文档。

    **不变量：返回值恒 ≤ n。** 初值刻意是 0 而不是 BANDS[0]——写成 BANDS[0] 的话
    n=50 会返回 100，即文档上声称的下限**高于实际值**，那是一句假声明。
    档位机制的全部意义就是「这句话不会因为数字长大而变假」，方向反了就全废。
    """
    picked = 0
    for b in BANDS:
        if b <= n:
            picked = b
    return picked


# ---------------- 甲类：纯文本派生 ----------------
# 每一项的口径都写死在这里。口径可以商量，但必须只有一处，且改口径要改这里。

def _count_files(pattern: str) -> int:
    """一律用递归口径（与 pytest 自身的发现口径一致）。此前几项非递归、另几项递归，
    两套口径混在一起：今天恰好一致，但只要有人在 `backend/tests/` 或 `skills/` 下开
    一个子目录，数字就会无声地变而没有任何信号（首审 #17）。"""
    root, _, tail = pattern.rpartition("/")
    base = REPO / root if root else REPO
    if not base.is_dir():
        return 0
    return sum(1 for p in base.rglob(tail) if not any(_is_skipped(x) for x in p.parts))


# 扫源码时必须排除的目录。虚拟环境尤其要命：任何第三方包只要有一行
# `class X(BaseTool)` 就会**静默虚增** tool_classes——一次与本仓无关的 pip install
# 就能改掉宪章里的数字。
# ⚠️ 按**精确目录名** `.venv` 排除是不够的：本仓还有 `backend/.venv-mlxsrc`
# （MLX 源码自建环境，6206 个 .py），它整个落在扫描面里，而当时那句注释还在替这个
# 没堵上的洞打包票（首审 Critical 2）。故改为按前缀与 venv 标记文件识别。
# 也不再按名字排除 `models`/`dist`/`build`：它们只在没有 .py 时无害，一旦某天出现
# 同名的源码包就会静默漏算，而排除它们本来就没换来什么。
_SKIP_EXACT = {"__pycache__", "node_modules", ".git", "site-packages"}
_SKIP_PREFIX = (".venv", "venv")


def _is_skipped(part: str) -> bool:
    return part in _SKIP_EXACT or part.startswith(_SKIP_PREFIX)


def _source_py(root):
    """root 下的源码 .py。刻意用 os.walk 而非 rglob：能在进入虚拟环境目录**之前**
    就剪枝，不必先列举六千个文件再逐个丢弃。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not _is_skipped(d)
                       and not (Path(dirpath) / d / "pyvenv.cfg").exists()]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def _count_in_section(cfg: str, key: str, item_re: str) -> int:
    """在 YAML 的 `<key>:` 小节内数条目。小节 = 从 `<key>:` 那行起，到下一个缩进
    不深于它的非空行为止。找不到小节回 0——0 与真实档位数不会相等，会被判为漂移，
    方向安全（不会伪装成「一致」）。"""
    m = re.search(rf"^(\s*){re.escape(key)}:\s*$", cfg, re.M)
    if not m:
        return 0
    indent = len(m.group(1))
    out, started = [], False
    for line in cfg[m.end():].splitlines():
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            if started:
                break
        started = True
        out.append(line)
    return len(re.findall(item_re, "\n".join(out), re.M))


def derive_text_quantities() -> dict[str, int]:
    mcp_dir = REPO / "backend" / "framework" / "mcp"
    tool_classes = 0
    class_re = re.compile(r"^class\s+\w+\(BaseTool\)", re.M)
    for p in _source_py(REPO / "backend"):
        try:
            tool_classes += len(class_re.findall(p.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue

    cfg_path = REPO / "config.yaml"
    cfg = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
    return {
        # 口径：backend/tests/ 下的 test_*.py 个数（不含 conftest 与辅助模块）
        "test_files": _count_files("backend/tests/test_*.py"),
        # 口径：mcp/ 递归下的 .py，**不含 __init__.py**（包声明不是「工具文件」）
        "mcp_files": sum(1 for p in _source_py(mcp_dir) if p.name != "__init__.py"),
        # 口径：全 backend 内 `class X(BaseTool)` 的声明数
        "tool_classes": tool_classes,
        # 口径：agents/ 下的 *_sandbox.py（物理隔离沙箱各一份执行器）
        "sandboxes": _count_files("backend/framework/agents/*_sandbox.py"),
        # 口径：仓库根 skills/ 下的 .md
        "skills": _count_files("skills/*.md"),
        # 口径：一条夹具 = 一份 *.events.jsonl
        "fixtures": _count_files("protocol/fixtures/*.events.jsonl"),
        # 口径：限定在各自小节**之内**再数。全文数 `^    - repo:` 看似等价（今天
        # 结果也一样），但将来任何一个同缩进的列表（第二张注册表、重排档位表）
        # 都会无声改掉口径（首审 #5）。
        "model_registry": _count_in_section(cfg, "model_registry", r"^    - repo:"),
        "rag_tiers": _count_in_section(cfg, "tiers", r"^    - id:"),
    }


# ---------------- 乙类：必须跑收集器 ----------------

def derive_golden_tests() -> tuple[int | None, str]:
    """返回 (项数或 None, 说明)。venv 不在或收集失败一律 None——绝不回落到旧值。"""
    py = REPO / "backend" / ".venv" / "bin" / "python"
    if not py.exists():
        return None, f"unknown（找不到 {py}；绝不回落到上次的数）"
    try:
        r = subprocess.run(
            [str(py), "-m", "pytest", "tests", "--collect-only", "-q"],
            cwd=REPO / "backend", capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"unknown（收集器跑不起来：{e}）"
    # ⚠️ 必须先看 returncode。收集期报错时 pytest 仍会打出一个「N tests collected」，
    # 但那个 N 是**残缺收集**的结果——把它当真值就是拿一个偏小的数去当权威，
    # 比 unknown 更坏（unknown 至少会响）。
    if r.returncode != 0:
        return None, f"unknown（收集器 rc={r.returncode}，收集不完整；绝不取残缺计数）"
    # 只认完整收集那一种形态。`no tests collected` 与 `N/M tests collected
    # (K deselected)` 都不匹配 → 落到 unknown，方向安全。
    m = re.search(r"^(\d+) tests collected", r.stdout, re.M)
    if not m:
        return None, f"unknown（收集器输出里找不到完整计数，rc={r.returncode}）"
    return int(m.group(1)), "pytest --collect-only"


# ---------------- 锚点：文档里那句话长什么样 ----------------
# 每条锚点 = {file, re, want（各捕获组的期望字符串）, keys（它消费了哪些派生值）, what}。
# 正则**必须唯一命中**：命中 0 次 = 文档被改写、锚点失效；命中多次 = 口径有歧义。
# 两种都记「跑不起来」而不是「无漂移」。

def anchors(q: dict[str, int], golden: int | None) -> list[dict]:
    gold_band = f"{band_floor(golden)}+" if golden is not None else None
    return [
        {
            "file": "CHARTER.md",
            "re": r"mcp/（(\d+) 文件 (\d+) 类）\+ agents/（(\d+) 沙箱",
            "want": [str(q["mcp_files"]), str(q["tool_classes"]), str(q["sandboxes"])],
            "keys": ["mcp_files", "tool_classes", "sandboxes"],
            "what": "mcp 文件数 / BaseTool 类数 / 沙箱数",
        },
        {
            "file": "CHARTER.md",
            "re": r"跑 `backend/tests/`（(\d+\+) 项金测试",
            "want": [gold_band],
            "keys": ["golden_tests"],
            "what": "金测试项数（档位下限）",
        },
        {
            "file": "CHARTER.md",
            "re": r"`backend/tests/`（\*\*(\d+\+)\*\* 金测试）",
            "want": [gold_band],
            "keys": ["golden_tests"],
            "what": "金测试项数（档位下限）",
        },
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="文档规模数字的派生与校验（波 A3）")
    # `--check` 是目前唯一的模式（默认也是它）。**刻意没有 `--write`**：
    # 首批三处锚点全在 CHARTER.md，而它永不被自动改写——那样的 `--write` 可证明
    # 什么都不做，宣传一个不干活的旗标是同一个病的缩微版。等出现第一个可写文档的
    # 锚点时再加，连同它的金测试一起。
    ap.add_argument("--check", action="store_true",
                    help="校验并按三值退出码报告（默认行为，给不给都一样）")
    ap.parse_args()

    q = derive_text_quantities()
    golden, gold_note = derive_golden_tests()

    all_anchors = anchors(q, golden)
    consumed = {k for a in all_anchors for k in a["keys"]}

    # 派生 ≠ 校验。首批锚点只落在 CHARTER 三处，其余派生值今天**没有任何锚点在消费**
    # ——逐行标出来，别让这份输出读起来像「这些都核对过了」（首审 #4）。
    print("── 派生值 ──（✓ = 有锚点在校验它；其余只是备用原料）")
    for k, v in q.items():
        print(f"  {'✓' if k in consumed else ' '} {k:<16} {v}")
    tag = "✓" if "golden_tests" in consumed else " "
    if golden is None:
        print(f"  {tag} {'golden_tests':<16} {gold_note}")
    else:
        print(f"  {tag} {'golden_tests':<16} {golden}  → 文档写 {band_floor(golden)}+（{gold_note}）")

    rc = RC_PASS
    drift, broken = [], []
    for a in all_anchors:
        path = REPO / a["file"]
        if not path.exists():
            broken.append(f"{a['file']}：文件不存在")
            continue
        text = path.read_text(encoding="utf-8")
        hits = list(re.finditer(a["re"], text))
        if len(hits) != 1:
            broken.append(f"{a['file']}：锚点命中 {len(hits)} 次（应为 1）— {a['what']}\n"
                          f"            正则 {a['re']}")
            continue
        if any(w is None for w in a["want"]):
            broken.append(f"{a['file']}：{a['what']} 派生不出真值（{gold_note}）")
            continue
        got = list(hits[0].groups())
        if got != a["want"]:
            line = text[: hits[0].start()].count("\n") + 1
            drift.append(f"{a['file']}:{line}  {a['what']}\n"
                         f"            文档写 {got} → 实际 {a['want']}")

    print("\n── 校验 ──")
    if broken:
        rc = RC_INTERNAL
        for b in broken:
            print(f"  ⚠️ 跑不起来  {b}")
    if drift:
        rc = RC_INTERNAL if broken else RC_DRIFT
        for d in drift:
            print(f"  ❌ 漂移      {d}")
    if not broken and not drift:
        print("  ✅ 全部一致")

    if drift:
        skipped = sorted({d.split(":")[0] for d in drift} & NEVER_WRITE)
        if skipped:
            print(f"\n[doc-numbers] {'、'.join(skipped)} 按宪章纪律**永不自动改写**——"
                  f"上面的 diff 须交用户确认后手改。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
