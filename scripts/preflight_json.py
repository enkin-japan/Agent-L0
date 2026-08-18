#!/usr/bin/env python3
"""把 `devctl preflight` 采到的事实 TSV 拼成 JSON（波 A1）。

**本脚本不做任何探测**——探测全在 `devctl.sh` 的 `preflight_facts` 里，且那里只调用
`scripts/lib_edge.sh` 已有的原语（`listen_pids` / `managed_or_report` / `probe_auth`）。
把拼装拆出来只是因为 bash 3.2 拼 JSON 的转义太容易出错，不是为了多一处判据。

stdin：`段<TAB>键<TAB>值<TAB>类型` 每行一条，**最后一行必须是完整性哨兵**
       `meta<TAB>complete<TAB>true<TAB>bool`。
stdout：`{schema, status, blockers[], needs_confirmation[], waived[], facts{}}`。

退出码（与 `devctl check` 同一套三值纪律）：
    0 = 可开工（可能带「须人工确认」项）／ 3 = 有阻断项 ／ 1 = 跑不起来（输入不成形）

两条设计要点，都是「不许把不知道说成没问题」的具体形态：

**① 哨兵不到就是跑不起来。** 采集函数在 `set -u` 下任一未定义变量即可半途死掉，
而管道左侧的死亡不会传给右侧（没有 `pipefail`）。只判 `rows == 0` 是不够的：
截断的事实流会拼出一份**只有半截 facts 的 `status: ready`**——一张空头保证书。
故要求采集侧在末尾打哨兵，见不到即 rc=1。

**② 判据只认 ASCII token，不认中文散文。** 值的格式是 `TOKEN：中文说明`，本脚本
只读冒号前那一段。原先按中文前缀判阻断，改个措辞就会把阻断静默降级成通过。
`_KNOWN_TOKENS` 把词汇表钉死：出现表外的 token 一律 rc=1，**绝不当成没问题**。

`needs_confirmation` 刻意不抬到非零：GPU 占用在本机几乎恒为 `unknown`（采集侧纪律
③），若让它把退出码变红，这道门就天天红，最终必被删掉。
"""
from __future__ import annotations

import json
import sys

RC_PASS, RC_INTERNAL, RC_BLOCKED = 0, 1, 3

SCHEMA = "agent-l0/preflight/1"
SENTINEL = ("meta", "complete")

# 每个「带状态的字段」允许出现的 token。表外 token = 采集侧改了词汇却没同步这里，
# 那时判据已经失效，必须响亮地报跑不起来而不是默默放行。
_KNOWN_TOKENS = {
    "venv": {"ok", "missing", "dangling", "present", "absent"},
    "ports": {"free", "managed", "foreign", "not_local", "busy"},
    "gpu_status": {"busy", "unknown"},
    "keys": {"present", "absent", "no_key", "not_running", "not_rejected", "FAIL"},
    "selection_host": {"ok", "empty_but_remote"},
}

# 这些字段是自由文本，不参与 token 校验
_FREE_TEXT = {
    ("venv", "test_cmd"), ("gpu", "evidence"), ("gpu", "process_scan"), ("gpu", "rule"),
    ("keys", "manual_only"), ("policy", "real_model_execution"),
    ("policy", "safe_concurrently"), ("repo", "root"), ("repo", "branch"), ("repo", "head"),
    ("selection", "model"), ("selection", "rag_tier"), ("selection", "inference_mode"),
}


def token(value: object) -> str:
    """取 `TOKEN：说明` 里的 token；没有冒号则整串就是 token。"""
    return str(value).split("：", 1)[0]


def _vocab_for(section: str, key: str) -> set[str] | None:
    if (section, key) in _FREE_TEXT:
        return None
    if section == "venv":
        return _KNOWN_TOKENS["venv"]
    if section == "ports":
        return _KNOWN_TOKENS["ports"]
    if section == "gpu" and key == "status":
        return _KNOWN_TOKENS["gpu_status"]
    if section == "keys":
        return _KNOWN_TOKENS["keys"]
    if section == "selection" and key == "infer_host":
        return _KNOWN_TOKENS["selection_host"]
    return None


def main() -> int:
    facts: dict[str, dict[str, object]] = {}
    saw_sentinel = False
    rows = 0
    bad_tokens: list[str] = []

    for raw in sys.stdin.read().splitlines():
        if not raw.strip():
            continue
        parts = raw.split("\t")
        if len(parts) != 4:
            print(f"[preflight] 事实行不成形（{len(parts)} 段，应为 4）: {raw!r}", file=sys.stderr)
            return RC_INTERNAL
        section, key, value, kind = parts
        if (section, key) == SENTINEL:
            saw_sentinel = (value == "true")
            continue
        if kind == "bool":
            v: object = (value == "true")
        elif kind == "int":
            try:
                v = int(value)
            except ValueError:
                v = value
        else:
            v = value
        vocab = _vocab_for(section, key)
        if vocab is not None and token(v) not in vocab:
            bad_tokens.append(f"{section}.{key} 的 token {token(v)!r} 不在词汇表 {sorted(vocab)}")
        facts.setdefault(section, {})[key] = v
        rows += 1

    # ① 哨兵：采集半途死掉时，拼出一份半截 facts 的 ready 就是一张空头保证书
    if not saw_sentinel:
        print(f"[preflight] 事实流不完整（收到 {rows} 行但没有完整性哨兵）"
              f"——采集可能半途失败，这不是「可开工」", file=sys.stderr)
        return RC_INTERNAL
    if rows == 0:
        print("[preflight] 没有收到任何事实行——这不是「可开工」", file=sys.stderr)
        return RC_INTERNAL
    # ② 词汇表：判据已失效时必须响亮，而不是默默按「不匹配任何阻断规则」放行
    if bad_tokens:
        for b in bad_tokens:
            print(f"[preflight] {b}", file=sys.stderr)
        print("[preflight] 采集侧词汇与本脚本的判据脱节——判据已不可信，这不是「可开工」",
              file=sys.stderr)
        return RC_INTERNAL

    blockers: list[str] = []
    needs: list[str] = []

    venv = facts.get("venv", {})
    if token(venv.get("backend", "")) == "missing":
        blockers.append(f"backend venv 缺失：{venv.get('backend')}")
    if token(venv.get("rag", "")) == "missing":
        # rag venv 只在动检索侧时才要，故不阻断，但必须让人看见
        needs.append(f"rag venv 缺失：{venv.get('rag')}——涉及 rag/ 的委托要先 setup")

    for port_key, state in facts.get("ports", {}).items():
        if token(state) == "foreign":
            blockers.append(f"{port_key}：{state}")

    for k, v in facts.get("keys", {}).items():
        if not k.endswith("_verified"):
            continue
        t = token(v)
        if t == "FAIL":
            blockers.append(f"{k}：{v}")
        elif t in ("not_running", "no_key", "not_rejected"):
            # not_rejected 也进这里：它**不是**「钥匙对」，只是「那道门没判否」
            needs.append(f"{k}：{v}")

    if token(facts.get("selection", {}).get("infer_host", "")) == "empty_but_remote":
        blockers.append(f"inference: {facts['selection']['infer_host']}")

    gpu = facts.get("gpu", {})
    if token(gpu.get("status", "")) == "unknown":
        needs.append(f"GPU 占用未知：{gpu.get('evidence', '')}")
    elif token(gpu.get("status", "")) == "busy":
        needs.append(f"GPU 正忙：{gpu.get('evidence', '')}——单 GPU 无抢占，不要再起第二个真模型进程")

    out = {
        "schema": SCHEMA,
        "status": "blocked" if blockers else ("ready_with_unknowns" if needs else "ready"),
        "blockers": blockers,
        "needs_confirmation": needs,
        # `waived` 由人在委托里填：写下「明知某项未确认仍决定开工」及其理由。
        # 留空数组而不是省略——省略会让人以为这个概念不存在。
        "waived": [],
        "facts": facts,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return RC_BLOCKED if blockers else RC_PASS


if __name__ == "__main__":
    sys.exit(main())
