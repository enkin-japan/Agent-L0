"""build 85 守卫变异证明的通用执行器：读一份清单（JSON 列表，每项 name / file / old / new / tests），
逐个：注入（old 必须在文件里恰好出现一次）→ 清 __pycache__ → 跑目标用例 → 逐字节还原并核 sha256。
判定：pytest 退出码 1（有用例失败）= RED；0 = SURVIVED；其它（2 收集 / 导入出错等）= BROKEN——
变异把文件改坏了不算证明。退出码 0 = 全部 RED。记录进提交说明（CLAUDE.md「变异证明」）。

用法（仓库根）：backend/.venv/bin/python benchmarks/build85_mutations/run_mutants.py \
        benchmarks/build85_mutations/specA.json [--only A5,A9] [--log 路径]
file 相对仓库根；tests 相对 backend/（pytest 在 backend/ 下跑），可含 -k 表达式。
"""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
PY = str(BACKEND / ".venv/bin/python")


def _clear_pycache() -> None:
    for d in list(REPO.rglob("__pycache__")):
        if ".venv" in d.parts or ".git" in d.parts or "node_modules" in d.parts:
            continue
        shutil.rmtree(d, ignore_errors=True)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> int:
    args = sys.argv[1:]
    spec = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    only = None
    log = None
    if "--only" in args:
        only = set(args[args.index("--only") + 1].split(","))
    if "--log" in args:
        log = open(args[args.index("--log") + 1], "a", encoding="utf-8")
    rows = []
    for m in spec:
        key = m["name"].split()[0]
        if only and key not in only:
            continue
        path = REPO / m["file"]
        orig = path.read_bytes()
        text = orig.decode("utf-8")
        n = text.count(m["old"])
        if n != 1:
            rows.append((m["name"], f"NOT_APPLIED(old×{n})", ""))
            continue
        try:
            path.write_bytes(text.replace(m["old"], m["new"]).encode("utf-8"))
            _clear_pycache()
            p = subprocess.run(
                [PY, "-m", "pytest", *m["tests"], "-q", "--no-header", "-rfE",
                 "-p", "no:cacheprovider"],
                cwd=BACKEND, capture_output=True, text=True, timeout=1500)
            out = p.stdout + p.stderr
            verdict = {0: "SURVIVED", 1: "RED"}.get(p.returncode, f"BROKEN(rc={p.returncode})")
            red = [ln.split(" - ")[0] for ln in out.splitlines()
                   if ln.startswith(("FAILED ", "ERROR "))]
            tail = out.strip().splitlines()[-1] if out.strip() else ""
        except subprocess.TimeoutExpired:
            verdict, red, tail, out = "BROKEN(timeout)", [], "", ""
        finally:
            path.write_bytes(orig)
            _clear_pycache()
        restored = "restored" if _sha(path.read_bytes()) == _sha(orig) else "RESTORE_FAILED"
        rows.append((m["name"], f"{verdict} {restored}", f"{len(red)} 红；{tail}"))
        if log:
            log.write(f"=== {m['name']} → {verdict} {restored}\n" + "\n".join(red) + f"\n{tail}\n")
            log.flush()
        print(f"{m['name']} → {verdict} {restored} | {len(red)} 红 | {tail}", flush=True)
    bad = [r for r in rows if not r[1].startswith("RED restored")]
    print(f"\n合计 {len(rows)} 个变异，非 RED：{len(bad)}")
    for r in bad:
        print("  ✗", r[0], r[1])
    return 0 if rows and not bad else 1


if __name__ == "__main__":
    sys.exit(main())
