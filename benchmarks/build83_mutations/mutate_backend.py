"""build 83 后端守卫变异证明（B1 流式复读护栏 / B2 spill 短句柄 / B3 token 分解 / B4 PATCH 鉴权 /
出网 L0 腿 fail-closed）。每个变异：注入 → 清 __pycache__ → 跑目标用例 → 逐字节还原并核 sha256。
退出码 0 = 每个变异都至少有一条用例变红。记录进提交信息（CLAUDE.md「变异证明」）。

用法：  backend/.venv/bin/python benchmarks/build83_mutations/mutate_backend.py
"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "backend"
PY = str(ROOT / ".venv/bin/python")
RG = "framework/core/repetition_guard.py"
ORCH = "framework/core/orchestrator.py"
SPILL = "framework/core/spill.py"
ENG = "framework/core/engine.py"
SF = "framework/core/stream_filter.py"
ROUTES = "framework/api/routes.py"
EGRESS = "framework/cloud/egress_guard.py"

MUTANTS = [
    ("M1 ngram threshold >= -> <", RG, [("if ratio >= self.max_dup_ratio:", "if ratio < self.max_dup_ratio:")],
     ["tests/test_repetition_guard.py"]),
    ("M2 detector always None", RG, [("        if not text:\n            return None\n        st = ",
                                      "        if True:\n            return None\n        st = ")],
     ["tests/test_repetition_guard.py"]),
    ("M3 channel buffers merged", RG, [("        st = self._state.get(channel)\n        if st is None:\n"
                                        "            st = self._state[channel] = _ChannelState()",
                                        "        st = self._state.get(\"_all\")\n        if st is None:\n"
                                        "            st = self._state[\"_all\"] = _ChannelState()")],
     ["tests/test_repetition_guard.py"]),
    ("M4 nudge-only, never fail", ORCH, [("                if repetition_hits >= 2:\n",
                                          "                if False:\n")],
     ["tests/test_repetition_guard.py"]),
    ("M5 realpath containment removed", SPILL,
     [("        if os.path.commonpath([root, target]) != root or target == root:\n            return None\n",
       "        if False:\n            return None\n")],
     ["tests/test_file_extract_spill.py"]),
    ("M6 miscopied-path fallback removed", SPILL,
     [("        if f\"/{_SPILL_DIRNAME}/\" not in raw or not raw.endswith(\".txt\"):\n            return None\n",
       "        return None\n")],
     ["tests/test_file_extract_spill.py"]),
    ("M7 breakdown ignores pre-opened flag", ENG,
     [("        if enable_thinking and callable(_pre) and _pre(enable_thinking):\n",
       "        if False:\n")],
     ["tests/test_token_breakdown.py"]),
    ("M8 PATCH dependency back to require_owner", ROUTES,
     [("from .auth import require_owner_or_paired\n",
       "from .auth import require_owner, require_owner_or_paired\n"),
      ("              dependencies=[Depends(require_owner_or_paired)])\n",
       "              dependencies=[Depends(require_owner)])\n")],
     ["tests/test_approval_policy.py", "-k", "paired or mounts or shared_key or revoked"]),
    # 一审补：工具调用块内文本不再喂护栏（qwen35 失控工具参数在本部署上没有约束采样兜底）
    ("M9 on_tool_text wiring cut", SF,
     [("                if self.on_tool_text is not None:\n                    tool_text.append(self.buf[i])\n",
       "")],
     ["tests/test_repetition_guard.py"]),
    # 一审补：行判据不再要求含字母/汉字 → 全零矩阵 / 空模板表格被误杀
    ("M10 line rule alpha requirement removed", RG,
     [("                    and sum(ch.isalpha() for ch in s) >= _MIN_LINE_ALPHA):\n",
       "                    and True):\n")],
     ["tests/test_repetition_guard.py"]),
    # 一审补：出网 L0 腿被护栏打断时不再抛错 → 空文本被当成「没有敏感段」静默放行
    ("M11 egress model leg fail-open on repetition hit", EGRESS,
     [("    if r.get(\"repetition_guard\"):\n", "    if False:\n")],
     ["tests/test_egress_repetition_guard.py"]),
]


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def clear_pycache():
    for d in ROOT.rglob("__pycache__"):
        if ".venv" not in d.parts:
            shutil.rmtree(d, ignore_errors=True)


def main():
    rows = []
    for label, rel, edits, target in MUTANTS:
        path = ROOT / rel
        orig = path.read_bytes()
        before = sha(path)
        text = orig.decode("utf-8")
        for old, new in edits:
            assert text.count(old) == 1, (label, old)
            text = text.replace(old, new)
        try:
            path.write_bytes(text.encode("utf-8"))
            clear_pycache()
            r = subprocess.run([PY, "-m", "pytest", *target, "-q", "-p", "no:cacheprovider",
                                "-rf", "--no-header"], cwd=ROOT, capture_output=True, text=True)
            failed = [l.split("::", 1)[1].split(" ")[0] for l in r.stdout.splitlines()
                      if l.startswith("FAILED ")]
            rows.append((label, r.returncode, failed, r.stdout.strip().splitlines()[-1]))
        finally:
            path.write_bytes(orig)
        assert sha(path) == before, f"restore failed: {rel}"
        rows[-1] += ("restored sha256 " + before[:12],)
    clear_pycache()
    for label, rc, failed, summary, restored in rows:
        print(f"## {label}\n  rc={rc} {summary}\n  {restored}")
        for f in failed:
            print(f"  RED {f}")
    sys.exit(0 if all(rc != 0 for _, rc, *_ in rows) else 1)


if __name__ == "__main__":
    main()
