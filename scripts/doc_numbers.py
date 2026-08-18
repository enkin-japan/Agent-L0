#!/usr/bin/env python3
"""文档里的规模数字改为从仓库派生（波 A3 ≡ OSS 扫描 §3 A3）。

病根：`CHARTER.md` 是每个新会话的第一读物，而它第一页的规模判断是错的——写「600+
金测试」「mcp/ 27 文件 55 类」，而实际值高出一大截（金测试那项差了两倍有余）。
散文里的数字没有任何机制会去更新它，只会越放越旧。
**本文件里刻意不复述任何具体数字**——一个专门消灭写死数字的工具，自己写死一份就是
笑话（首审 #11：这里原本硬编了三个数，而它们已经过期）。当前真值见台账 §2 那张表。

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

打点宿主是 `EXECUTION_LEDGER.md` §2 的「当前规模」表——**不是 `CHARTER.md`**。
2026-08-18 用户拍板把精确数字整个挪出宪章、只留指向台账的指针：宪章是每个新会话的
第一读物，而它每改一次都要用户显式同意，把易变的数字放在那里等于造一道天天要拍板
的门。宿主可写之后，`--write` 一条命令即可刷新，不必再为「加了几条测试」惊动用户。

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

# 宪章的任何改动都要用户显式同意（CLAUDE.md 红线）。
# ⚠️ 如实说明现状（首审 Critical 4 + #9）：本脚本**会**就地改写 `EXECUTION_LEDGER.md`
# （`--write`），只是**永不**改写下面这张名单里的文件。而今天所有锚点都落在台账上，
# 所以这张名单**当前没有活路径会命中**——它是**回归网**，不是正在生效的执法：
# 将来若有人把锚点又指回宪章，`--write` 会在这里被挡住，而不是默默写下去。
NEVER_WRITE = {"CHARTER.md"}

# 档位下限（`1000+`）那套机制已随宿主一起退休：它是**宪章时期的妥协**——精确值每加
# 一条测试就过期，而宪章每改一次都要用户同意，只好写成一个跨档才变的近似。宿主换成
# 台账后精确值可由 `--write` 一条命令刷新，档位就成了看着像活着的死代码，故删除。
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

# 锚点全部落在 `EXECUTION_LEDGER.md` 的「当前规模」表上。
# **不再打点 CHARTER.md**：2026-08-18 用户拍板把精确数字整个挪出宪章、只留指向台账
# §2 的指针——宪章是每个新会话的第一读物，而它每改一次都要用户显式同意，把易变的
# 数字放在那里等于造一道天天要拍板的门。
_LEDGER = "EXECUTION_LEDGER.md"

# (派生值键, 表格里的行名)。行名必须与台账里逐字一致。
_ROWS = [
    ("golden_tests", "金测试项数"),
    ("test_files", "测试文件数"),
    ("mcp_files", "mcp 文件数"),
    ("tool_classes", "BaseTool 子类"),
    ("sandboxes", "物理隔离沙箱"),
    ("skills", "剧本"),
    ("fixtures", "协议夹具"),
    ("model_registry", "model_registry 档位"),
    ("rag_tiers", "rag.tiers 档位"),
]


def anchors(q: dict[str, int], golden: int | None) -> list[dict]:
    """每行一个锚点：`| <行名> | <数> | <口径…> |`。

    金测试项数在这里用**精确值**而非档位下限——宿主换成台账后它可写了，`--write`
    一条命令就能刷新，不必再为「加了几条测试」去惊动用户。档位下限那套是宪章时期的
    妥协，随宿主一起退休。
    """
    out = []
    values = dict(q)
    values["golden_tests"] = golden
    for key, label in _ROWS:
        v = values.get(key)
        out.append({
            "file": _LEDGER,
            # 行名两侧的 `|` 与空格逐字钉住，免得匹配到正文里同名的字
            "re": rf"^\| {re.escape(label)} \| (\d+) \|",
            "want": [None if v is None else str(v)],
            "keys": [key],
            "what": label,
        })
    return out


def rewrite(path: Path, drift_specs: list[tuple[str, str, str]]) -> "int | None":
    """按 (正则, 旧值, 新值) 就地改写；全部命中返回改动数，**任一项对不上返回 None
    且一个字都不写**。

    只替换**捕获组那一段**，不重排整行——口径列是人写的散文，绝不能被机器覆盖掉。

    三条首审订正（#12）：
      ① `old` 此前收了却从不用，于是「即将覆盖的文本还是不是校验时看到的那个值」
         这道保险**根本不存在**。现在逐条核对，对不上就整体放弃。
      ② 命中不到时此前静默 `continue`，`n` 少算而调用方照样打「已刷新 N 处」。
      ③ 零命中时此前也会 `write_text` 一遍：无谓的 mtime 抖动，且 CRLF 会被
         read/write 往返归一成 LF。
    """
    text = path.read_text(encoding="utf-8")
    pending = []
    for pattern, old, new in drift_specs:
        m = re.search(pattern, text, re.M)
        if not m or m.group(1) != old:
            return None                      # 文本已不是校验时的样子：整体放弃
        pending.append((m.span(1), new))
    if not pending:
        return 0                             # 无事可做时不碰文件
    # 从后往前替换：前面的替换会让后面已算好的 span 失效
    for (lo, hi), new in sorted(pending, key=lambda x: x[0][0], reverse=True):
        text = text[:lo] + new + text[hi:]
    path.write_text(text, encoding="utf-8")
    return len(pending)


def main() -> int:
    ap = argparse.ArgumentParser(description="文档规模数字的派生与校验（波 A3）")
    ap.add_argument("--check", action="store_true",
                    help="只校验并按三值退出码报告（默认行为）")
    # `--write` 此前刻意不存在：那时锚点全在 CHARTER.md，而它永不被自动改写，
    # 那样的旗标可证明什么都不做。宿主换成台账后它才有活干（2026-08-18）。
    ap.add_argument("--write", action="store_true",
                    help=f"就地把漂移的数字刷成实际值（只改数字，不动口径说明）。"
                         f"{'/'.join(sorted(NEVER_WRITE))} 永不改写。")
    args = ap.parse_args()
    # ★ `--check` 此前被解析却从不读，于是 `--check --write` 会照样改写治理文档，
    # 而用户以为自己要的是只读校验（首审 #5）。两者互斥，**只读的一方赢**。
    if args.check and args.write:
        print("[doc-numbers] --check 与 --write 互斥（只读的一方赢，什么都不会被改写）",
              file=sys.stderr)
        return RC_USAGE

    q = derive_text_quantities()
    golden, gold_note = derive_golden_tests()

    all_anchors = anchors(q, golden)
    consumed = {k for a in all_anchors for k in a["keys"]}

    # 派生 ≠ 校验：曾经只有四项被锚点消费，而输出的口吻像是都核对过（首审 #4）。
    # 换宿主后九项全部被消费，故今天这一列**应当全是 ✓**——留着它不是装饰，而是
    # 将来新增派生值却忘了给它加锚点时，那一行会缺 ✓ 而被
    # `test_every_derived_value_is_now_consumed_by_an_anchor` 当场抓住。
    print("── 派生值 ──（✓ = 有锚点在校验它；缺 ✓ = 派生了但没人核对）")
    for k, v in q.items():
        print(f"  {'✓' if k in consumed else ' '} {k:<16} {v}")
    tag = "✓" if "golden_tests" in consumed else " "
    if golden is None:
        print(f"  {tag} {'golden_tests':<16} {gold_note}")
    else:
        print(f"  {tag} {'golden_tests':<16} {golden}  （{gold_note}）")

    rc = RC_PASS
    drift, broken = [], []
    drift_specs: list[tuple[str, str, str, str]] = []
    for a in all_anchors:
        path = REPO / a["file"]
        if not path.exists():
            broken.append(f"{a['file']}：文件不存在")
            continue
        text = path.read_text(encoding="utf-8")
        # 锚点限定在「当前规模」小节之内。不限定的话，别处任何一张恰好有同名行的
        # 表格都会被认成锚点——重复会被「命中 != 1」拦下（fail-safe），但「本表被删、
        # 另一张表恰好有同名行」会**静默改指错的行**（首审 #18）。
        sec = re.search(r"^### 当前规模", text, re.M)
        if not sec:
            # ⚠️ **不回落到全文搜索**：小节不在就是打点面没了。回落的话，别处任何一张
            # 恰好有同名行的表都会被认成锚点，然后报「全部一致」——一次彻底的假绿
            # （首审 #18；实测：删掉本节后代码确实去认了另一张表并报一致）。
            broken.append(f"{a['file']}：找不到「### 当前规模」小节——打点面没了，"
                          f"这不是「没有漂移」")
            continue
        scope = text[sec.start():]
        offset = sec.start()
        hits = list(re.finditer(a["re"], scope, re.M))
        if len(hits) != 1:
            broken.append(f"{a['file']}：锚点命中 {len(hits)} 次（应为 1）— {a['what']}\n"
                          f"            正则 {a['re']}")
            continue
        if any(w is None for w in a["want"]):
            broken.append(f"{a['file']}：{a['what']} 派生不出真值（{gold_note}）")
            continue
        got = list(hits[0].groups())
        if got != a["want"]:
            line = text[: offset + hits[0].start()].count("\n") + 1
            drift.append(f"{a['file']}:{line}  {a['what']}\n"
                         f"            文档写 {got} → 实际 {a['want']}")
            drift_specs.append((a["file"], a["re"], got[0], a["want"][0]))

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

    # ★ 判据不可信时不许动文件：只要有一项锚点失配或派生不出真值，这份校验整体就
    # 不可信，此时改写别的行等于「在一份读不准的图纸上照着改」。宁可什么都不做。
    if broken and args.write:
        print("\n[doc-numbers] 有项目跑不起来（见上），**不改写任何文件**——"
              "判据不可信时动文件比不动更坏。先修好上面那几条。")
    elif drift and args.write:
        blocked = sorted({d.split(":")[0] for d in drift} & NEVER_WRITE)
        if blocked:
            print(f"\n[doc-numbers] {'、'.join(blocked)} 按宪章纪律**永不自动改写**——"
                  f"上面的 diff 须交用户确认后手改。")
        writable = [sp for sp in drift_specs if sp[0] not in NEVER_WRITE]
        n = 0
        for f in sorted({sp[0] for sp in writable}):
            got = rewrite(REPO / f, [(sp[1], sp[2], sp[3]) for sp in writable if sp[0] == f])
            if got is None:
                print(f"\n[doc-numbers] {f} 的文本已不是校验时的样子——**一个字都没改**。"
                      f"重跑 --check 再试。", file=sys.stderr)
                return RC_INTERNAL
            n += got
        print(f"\n[doc-numbers] 已就地刷新 {n} 处（共 {len(writable)} 处漂移）。"
              f"**请复跑 --check 确认归零**——改写自己说改成功了不算数。")
        # 刷完了就别再报「有漂移」：那既让 `--write && …` 永远走不下去，也让人以为
        # 什么都没修成（首审 #13）。但**刻意不回 0**——回 0 等于替一次未经复核的改写
        # 背书，而本文件通篇的规矩就是「自己说成功不算数」。留 3 表示「动过、待复核」。
        if n == len(writable):
            print("[doc-numbers] rc 仍为 3 = 「已改写、待复核」，不是「失败」。")
    elif drift:
        print("\n[doc-numbers] 用 `--write` 就地刷新（只改数字，不动口径说明）。")
    return rc


if __name__ == "__main__":
    sys.exit(main())
