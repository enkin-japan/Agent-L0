#!/bin/bash
# =======================================================
# PreToolUse(Bash) 守卫：拦住让 pre-commit 整套失效的提交写法（波 A0）
#
# 为什么要它：`.git/hooks/pre-commit` 挡得住「忘了跑门」，挡不住「把门一键关掉」。
# 本仓的病恰恰是**守卫静默地自己关掉自己**。这个钩子只约束 agent 走 Bash 工具发的
# 命令，不影响用户自己在终端里敲什么。
#
# 拦三种等效的「关掉整套钩子」：
#   · `git commit --no-verify`（含 git parse-options 允许的前缀缩写 `--no-verif…`）
#   · `git commit -n`（含 `-an` 这类短旗标簇）
#   · 任何 git 段里出现 `core.hooksPath`（`git -c core.hooksPath=/dev/null commit`
#     与 `--no-verify` 等效，`git config core.hooksPath` 更是永久性的）
#
# ⚠️ 契约的反直觉处（官方文档 + 本会话实证）：PreToolUse 只有 **exit 2** 阻断；
# 其它非零码是「非阻断错误」——**钩子自己崩了会放行**。所以：
#   · 主形态朝安全方向失败：切词失败（引号不闭合）时 python 回非零，由下面的原文
#     兜底接住，而不是「解析失败就放行」；
#   · 但**绝不允许存在恒 exit 2 的路径**——那会把整个会话锁死，比漏判严重得多。
#     故畸形/无关输入一律走放行分支。
#
# ⚠️ 一次真实的失效（首审揪出，已修，留档）：早先的实现先把 `&&|;` 和换行替换成
# 分隔符、再逐段 shlex 切词。这个**顺序本身是错的**——分隔符若出现在引号内部，
# 预切分会把命令切碎，随后 shlex 因引号不闭合抛 ValueError，而当时那条路径 return
# None（判为「不违规」）且 python 仍以 0 退出，于是外层原文兜底根本接不到。后果：
# `git commit -n -m "fix: a|b"`、以及**本仓 CLAUDE.md 强制的多行 commit message**
# （`Co-Authored-By:` / `Claude-Session:` 各占一行）全都一路放行——守卫在最主流的
# 形态上等于不存在，还有一条注释在保证自己没漏。
# 正解是**先整条切词、再按分隔符 token 分段**：shlex 天然懂引号与换行。
#
# 已知残留（明写，不假装没有）：python3 缺席/损坏时只剩整段原文 grep，能拦长旗标与
# core.hooksPath，短旗标 `-n` 会漏。macOS 自带 python3，此路径实际不可达。
# =======================================================
set -u

raw=$(cat)

# 零成本短路：绝大多数 Bash 调用与 git 提交无关，别给每条命令加 python3 的启动开销。
# ⚠️ 匹配面是**整段 JSON**（含 description 等字段），不是单独的 command——所以它是
# 个超集：会多跑几次 python，不会漏判。
case "$raw" in
    *commit*|*hooksPath*) ;;
    *) exit 0 ;;
esac

reason=$(printf '%s' "$raw" | python3 -c '
import json, shlex, sys

SEPS = {"&&", "||", ";", "|", "&", "(", ")", "{", "}", "\n"}
# 会「吃掉下一个 token」的 git 全局旗标
GLOBAL_VALUE_FLAGS = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
# 透明包裹层：真正的命令在它们后面
WRAPPERS = {"env", "sudo", "nohup", "time", "command", "nice", "stdbuf"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
# git commit 里「吃值」的短旗标：簇里一旦遇到它，其后全是这个旗标的值，不再是旗标。
# （没有这一条，`-m"none"` 被 shlex 切成 -mnone 就会因为含 n 被误拦）
SHORT_VALUE_FLAGS = set("mFcCtSu")


def tokenize(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)                      # 引号不闭合会抛 ValueError，由调用方上报


def segments(toks):
    cur, out = [], []
    for t in toks:
        if t in SEPS:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        out.append(cur)
    return out


def strip_prefix(toks):
    """剥掉 VAR=VAL 前缀与 env/sudo/nohup 这类透明包裹层。"""
    i = 0
    while i < len(toks):
        t = toks[i]
        if "=" in t and not t.startswith("-") and t.split("=", 1)[0].replace("_", "a").isalnum():
            i += 1
            continue
        if t.rsplit("/", 1)[-1] in WRAPPERS:
            i += 1
            while i < len(toks) and toks[i].startswith("-"):
                i += 1
            continue
        break
    return toks[i:]


def offends_git(toks):
    """toks[0] 已确认是 git。返回触发理由，或 None。"""
    i, sub = 1, None
    while i < len(toks):
        t = toks[i]
        if "core.hooksPath" in t:
            return "core.hooksPath"
        if t in GLOBAL_VALUE_FLAGS:
            if i + 1 < len(toks) and "core.hooksPath" in toks[i + 1]:
                return "core.hooksPath"
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        sub = t
        break
    if sub == "config" and any("core.hooksPath" in t for t in toks[i:]):
        # 只读查询与 --unset 不拦：前者无副作用，后者恰恰是**修好**它的动作。
        # 拦住修复动作会逼人绕过守卫，那是最坏的结果。
        benign = ("--get", "--get-all", "--get-regexp", "--list", "-l", "--unset", "--unset-all")
        if not any(t.startswith(benign) for t in toks[i:]):
            return "core.hooksPath"
    if sub != "commit":
        return None
    for t in toks[i + 1:]:
        if t.startswith("--no-verif"):        # git 允许无歧义前缀缩写
            return t
        if len(t) > 1 and t[0] == "-" and t[1] != "-":
            for ch in t[1:]:
                if ch == "n":
                    return t
                if ch in SHORT_VALUE_FLAGS:   # 其后是这个旗标的值，不再扫
                    break
    return None


def scan(cmd, depth=0):
    if depth > 3:
        return None
    for seg in segments(tokenize(cmd)):
        seg = strip_prefix(seg)
        if not seg:
            continue
        exe = seg[0].rsplit("/", 1)[-1]
        if exe in SHELLS:                     # `bash -c "git commit -n"`：递归进去
            for j, t in enumerate(seg[1:], 1):
                if t == "-c" and j + 1 < len(seg):
                    hit = scan(seg[j + 1], depth + 1)
                    if hit:
                        return hit
                    break
            continue
        if exe == "git":
            hit = offends_git(seg)
            if hit:
                return hit
    return None


try:
    cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "")
except Exception:
    sys.exit(9)                               # JSON 坏了：交给外层原文兜底
if not isinstance(cmd, str):
    sys.exit(9)                               # command 不是字符串：同上，绝不当放行
try:
    hit = scan(cmd)
except ValueError:
    sys.exit(9)                               # 引号不闭合，切不了词：交给外层兜底
except Exception:
    sys.exit(9)                               # 任何意外都不许变成「放行」
if hit:
    print(hit)
' 2>/dev/null)
py_rc=$?

if [ "$py_rc" -ne 0 ]; then
    # 兜底：整段原文里出现这两个串就拦。宁可偶尔误伤（换个写法即可），
    # 也不要在解析失败时静默放行。
    case "$raw" in
        *--no-verif*)   reason="--no-verify" ;;
        *core.hooksPath*) reason="core.hooksPath" ;;
        *) exit 0 ;;
    esac
fi

[ -n "$reason" ] || exit 0

echo "已拦下 git commit 的 ${reason}：它会把 pre-commit 里的 devctl check 整套关掉。" >&2
echo "本仓的规矩是——门跑不起来或没过就不提交，而不是绕过门。" >&2
echo "先跑 ./devctl.sh check 定位（rc=3 判否 / rc=1 跑不起来），修好再正常 git commit。" >&2
echo "确有必要跳过（例如门本身正在被修），请让用户在终端里自己敲那条命令。" >&2
exit 2
