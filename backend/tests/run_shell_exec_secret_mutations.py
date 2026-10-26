"""shell_exec 密钥隔离护栏的变异证明（一条命令跑完，不进 pytest 收集：文件名不以 test_ 开头）。

用法（在 backend/ 下）：
    PYTHONUNBUFFERED=1 <venv>/bin/python tests/run_shell_exec_secret_mutations.py

每个变异：记 sha256 → 注入（被替换片段必须恰好出现一次）→ 清 __pycache__ → 跑指定测试
→ 期望红 → 按字节还原 → 校验 sha256。先跑一遍未变异的同组测试确认是绿的（证明测试
真被装载、且红是变异造成的）。任何一条还原校验失败即非零退出。
"""
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
TARGET = BACKEND / "framework" / "mcp" / "tools" / "shell_exec.py"
TEST_FILE = "tests/test_shell_exec_approval.py"

# (名字, 原片段, 变异片段, 期望变红的测试 -k 表达式, 说明)
MUTANTS = [
    ("m1", "    if strict:\n        env = {k: v for k, v in os.environ.items() if not _is_secret_env_name(k)}",
     "    if strict:\n        env = dict(os.environ)", "test_child_env_drops_secret_variables",
     "环境剔除恢复为 dict(os.environ)"),
    ("m2", '_SECRET_BASENAME_PREFIXES = (".env.",)', "_SECRET_BASENAME_PREFIXES = ()",
     "test_secret_paths_require_approval", ".env.* 前缀规则不命中"),
    ("m3", '_SECRET_SUFFIXES = (".p8", ".pem", ".key", ".p12")',
     '_SECRET_SUFFIXES = (".pem", ".key", ".p12")',
     "test_secret_paths_require_approval", ".p8 后缀不命中"),
    ("m4", '"ps": ("Aacefhjlmruvwx",', '"ps": ("AEacefhjlmruvwx",',
     "test_ps_environment_display_requires_approval", "ps 允许选项恢复 E"),
    ("m5", '    if not text:\n        return text\n    pairs = sorted(',
     '    return text\n    pairs = sorted(',
     "test_redaction_replaces_secret_values or test_tool_output_is_redacted_end_to_end",
     "脱敏返回原文"),
    ("m6", '_SECRET_BASENAMES = frozenset({".env", ', "_SECRET_BASENAMES = frozenset({",
     "test_secret_paths_require_approval", ".env 精确名不命中"),
    ("m7", 'if head == "ps" and any(c in "eE" for a in operands for c in a):',
     'if False:', "test_ps_environment_display_requires_approval",
     "无横杠 ps e/auxe 不挡"),
    ("m8", "        return any(fnmatch.fnmatchcase(s, name) for s in _SECRET_NAME_SAMPLES)",
     "        return False", "test_secret_paths_require_approval", "通配模式不命中"),
    ("m9", 'if cwd and not c.startswith(("/", "~")):', "if False:",
     "test_secret_path_via_cwd_requires_approval", "工作目录不参与判定"),
    ("m10", "if any(_secret_path_hit(f) for f in forms):", "if False:",
     "test_secret_paths_require_approval", "词元层规则整体失效"),
    ("m11", "if head in _RECURSIVE_READERS and _recursive_read_needs_approval(head, args, spec, cwd):",
     "if False:", "test_broad_recursive_search_requires_approval or test_recursive_root_resolution_requires_approval",
     "递归读取规则整体失效"),
    # 只看 test_recursive_root_resolution_requires_approval：那组刻意不含 `.`/`~`/`/` 字面量，
    # 旧字面量护栏挡不住它们，变红才算根路径解析的证据。
    ("m12", "return not all(_recursive_root_allowed(_abs_search_path(p, base)) for p in roots)",
     'return any(p == "." for p in roots)', "test_recursive_root_resolution_requires_approval",
     "根路径解析失效（只认字面 .）"),
    ("m13", '"grep": ("EFGHILPRUVabchilnqrsuvwxZ", "defmABC", {',
     '"grep": ("EFGHILPRUVabcdhilnqrsuvwxZ", "efmABC", {',
     "test_grep_directories_recurse_is_recursive", "grep -d 恢复为无值选项"),
    ("m14", '"rg": True, "ag": True, "ack": True, "diff": False}', '"rg": True, "ag": True, "ack": True}',
     "test_recursive_diff_requires_approval", "diff 不在递归读取集合"),
    ("m15", "    if any(_is_fold_hazard(ch) for t in tokens for ch in t):\n        return False",
     "    if False:\n        return False", "test_non_ascii_tokens_require_approval",
     "可折叠非 ASCII 不挡（全部非 ASCII 放行）"),
    ("m21", "if any(_is_fold_hazard(ch) for t in tokens for ch in t):",
     "if any(not t.isascii() for t in tokens):", "test_plain_non_ascii_text_stays_automatic",
     "中文也挡（误伤回归）"),
    ("m22", '"backend/push", "backend/registrations", ".colab-compute", "reminder")',
     '"backend/push", "backend/registrations", ".colab-compute", "reminder", "apple-app")',
     "test_apple_app_tree_is_not_a_secret_location", "整棵 apple-app 又算密钥位置（误伤回归）"),
    ("m16", "excluded = _pattern_token_indices(tokens)", "excluded = set()",
     "test_search_pattern_is_not_treated_as_a_path", "模式词元被当成路径（误伤回归）"),
    ("m17", "if k.upper() not in _BACKEND_OWN_KEYS}", "if not _is_secret_env_name(k)}",
     "test_approved_commands_keep_user_tokens_but_never_backend_keys", "审批后 env 仍剔 HF_TOKEN"),
    ("m18", "env=_user_env() if readonly else _user_env(strict=False),", "env=_user_env(strict=False),",
     "test_child_env_drops_secret_variables", "execute 不按审批判定选 env（总用宽松版）"),
    ("m19", "for fn in (os.path.normpath, os.path.realpath):",
     "for fn in (os.path.normpath,):",
     "test_recursive_root_follows_symlinks_and_home_locations", "不按 realpath 复核（符号链接出仓库）"),
    ("m20", "candidates.extend(token[i:] for i in range(2, len(token)))",
     "candidates.extend(token[i:] for i in range(2, 3))",
     "test_secret_paths_require_approval", "粘连短选项只切 token[2:]"),
    ("m23", '_ROOT_SECRET_LOCATIONS = (".hf_token_tmp", ".keys", "backend/api_tools", "backend/auth",',
     '_ROOT_SECRET_LOCATIONS = (".hf_token_tmp", ".keys", "backend/auth",',
     "test_backend_api_tools_is_a_secret_location", "去掉 backend/api_tools"),
    ("m24", "        if data:\n            data_f = fn(data).lower()",
     "        if False:\n            data_f = fn(data).lower()",
     "test_data_dir_secret_subdirs_follow_agent_data_dir", "data_dir 位置失效"),
    ("m25", "        if _under_secret_location(_abs_search_path(c, _search_base(cwd))):",
     "        if False:", "test_backend_api_tools_is_a_secret_location or "
     "test_data_dir_secret_subdirs_follow_agent_data_dir or test_app_support_data_root_is_a_fixed_home_location",
     "词元层不按绝对路径比密钥位置"),
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clear_pycache() -> None:
    subprocess.run('find . -name __pycache__ -type d -not -path "*/.venv/*" -exec rm -rf {} +',
                   shell=True, cwd=BACKEND, check=False)


def run_tests(k_expr: str):
    clear_pycache()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    r = subprocess.run([sys.executable, "-m", "pytest", TEST_FILE, "-q", "-rf",
                        "-p", "no:cacheprovider", "-k", k_expr],
                       cwd=BACKEND, capture_output=True, text=True, env=env)
    failed = sorted({m.group(1).split("::", 1)[-1].split("[", 1)[0]
                     for m in re.finditer(r"^FAILED (\S+)", r.stdout, re.M)})
    nfail = len(re.findall(r"^FAILED ", r.stdout, re.M))
    summary = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
    return r.returncode, failed, nfail, summary


def main() -> int:
    original = TARGET.read_bytes()
    original_sha = sha256(TARGET)
    rows, ok = [], True

    all_k = " or ".join(sorted({m[3] for m in MUTANTS}))
    code, _, _, summary = run_tests(all_k)
    print(f"[基线] 未变异 rc={code} {summary}")
    if code != 0:
        print("基线不绿，变异结果无意义，退出。")
        return 2

    for name, old, new, k_expr, desc in MUTANTS:
        text = original.decode("utf-8")
        count = text.count(old)
        if count != 1:
            rows.append((name, desc, f"注入失败：片段出现 {count} 次", "-", "-"))
            ok = False
            continue
        try:
            TARGET.write_bytes(text.replace(old, new).encode("utf-8"))
            assert sha256(TARGET) != original_sha
            code, failed, nfail, _summary = run_tests(k_expr)
        finally:
            TARGET.write_bytes(original)
        restored = sha256(TARGET) == original_sha
        ok = ok and restored and code != 0
        verdict = f"红（{nfail} 例）: " + ", ".join(failed) if code != 0 else "存活（未变红）"
        rows.append((name, desc, verdict, "是" if restored else "否！", "killed" if code else "SURVIVED"))

    clear_pycache()
    print("\n| mutant | 变异 | 变红的测试 | 已还原(sha256) | 结论 |")
    print("|---|---|---|---|---|")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    final_sha = sha256(TARGET)
    print(f"\n目标文件 sha256 {'一致' if final_sha == original_sha else '不一致！'}: {final_sha}")
    gs = subprocess.run(["git", "status", "--short"], cwd=BACKEND, capture_output=True, text=True)
    print("git status --short:\n" + (gs.stdout.rstrip() or "(clean)"))
    return 0 if ok and final_sha == original_sha else 1


if __name__ == "__main__":
    sys.exit(main())
