"""检索质量评估跑批（数据架构采纳波 R1）：防负优化基线。

用法（rag venv）：
  .venv/bin/python evals/run_retrieval_eval.py
      [--save-baseline] [--keep] [--floor-margin F]
      [--no-enforce-floor] [--allow-floor-drop --reason "…"]
**退出码**：0 = 不低于地板且无逐题由过转败；1 = 判为回归 / 档位解析不出 /
拒绝把回归钉成新基线。调用方请检查 `$?`（目前尚无 CI 或 devctl 入口调它，
见 EXECUTION_LEDGER「存量缺陷修正收案」块记的后续项）。

机制：隔离数据根（临时目录）+ 融合模式拉起真实 rag_server（部署同款 HTTP 面），
档位解析与服务端同源：仓库根 `.rag-tier-selection` 指针 → `config.yaml
rag.active_tier`（指针在 .gitignore 里，新克隆没有它，只读指针会让棘轮自我关闭；
2026-08-17 为 vl-2b。此前这里写死「0.6b 档」是陈旧注释，实现从 R1 起就是复制指针），
灌入 corpus.jsonl 夹具语料（强制分类保证确定性），跑 items.jsonl 固定题集，按命中
文档 title 判分（Recall / MRR / 分形态通过率）。产物 results/<时间戳>.json（不入库），
与 BASELINE.json（入库）对比打印增减——**任何检索侧改动后先跑这个**，变差即回退或解释。

判分词汇（items.jsonl expect）：
  hit_any        列表内至少一篇进 top-k
  hit_all        全部进 top-k（可带 top_k 放宽名额）
  empty_ok       允许空结果（软判据）
  require_empty  **必须**返回空——库外问题返回任何文档都算硬凑（negative 一律用它；
                 miss_all 是枚举式的，只列作者想得到的干扰项，天然会漏）
  miss_all       列表内的文档出现即失败（作为 require_empty 的补充说明保留）
  stale_not_top1 该文档若排第 1 即失败（时效裁决探针：旧事实不应压过新事实）
临时服务端口 14967；用完即收（CHARTER 部署纪律）。
"""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests
import yaml

HERE = Path(__file__).resolve().parent
RAG_ROOT = HERE.parent
REPO_ROOT = RAG_ROOT.parent
PORT = 14967
BASE = f"http://127.0.0.1:{PORT}"

# 分类名必须 ASCII（Chroma collection 命名约束，与生产部署同）；路由语义在描述里
CATEGORY_DESCRIPTIONS = {
    "device_tech": "设备与技术：路由器、网络、电脑、汽车等设备的配置、固件、备份与维修保养记录",
    "household": "家庭事务：房租、保险、孩子接送、住址、家电维护等家庭日常事务",
    "health_life": "健康与生活：家人健康记录、食谱烘焙、旅行订房等生活信息",
}


def _load_jsonl(path: Path):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out


def _wait(pred, timeout: float, interval: float = 1.0, what: str = ""):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"等待超时: {what}")


def start_server(tmp: Path) -> subprocess.Popen:
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg.setdefault("rag", {})
    cfg["rag"]["host"] = "127.0.0.1"
    cfg["rag"]["port"] = PORT
    cfg["rag"]["compute"] = {**(cfg["rag"].get("compute") or {}), "host": ""}
    cfg_path = tmp / "eval_config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    # 档位指针随行：compute_core 按 CONFIG_PATH.parent 找 .rag-tier-selection，
    # RAG_CONFIG 指到 tmp 后指针必须复制过来——否则永远落回 active_tier 默认，
    # "五档连评"实为同一档跑五遍（2026-08-08 五档全同分案，8B 装载时长露馅）
    sel = REPO_ROOT / ".rag-tier-selection"
    if sel.exists():
        (tmp / ".rag-tier-selection").write_text(sel.read_text(encoding="utf-8"),
                                                 encoding="utf-8")

    env = {**os.environ,
           "RAG_CONFIG": str(cfg_path),
           "RAG_DATA_DIR": str(tmp / "data"),
           "RAG_COMPUTE_HOST": "",      # 清掉 .env.local 可能带来的拆分覆盖：评估恒融合
           "RAG_API_KEY": ""}
    proc = subprocess.Popen([str(RAG_ROOT / ".venv/bin/python"), "rag_server.py"],
                            cwd=str(RAG_ROOT), env=env,
                            stdout=(tmp / "server.log").open("w"),
                            stderr=subprocess.STDOUT)
    try:
        _wait(lambda: requests.get(f"{BASE}/health", timeout=2).json().get("status") == "healthy",
              timeout=240, what="rag_server /health（含模型加载）")
    except Exception:
        # 起服失败也必须彻底收干净：只 terminate 不 wait 会留下孤儿 rag_server 抱着
        # 嵌入/重排权重和 :14967 不放（本文件 finally 处的注释自陈「真实推理后的服务端
        # 在 SIGTERM 下可能 >30s 才退」——terminate 完就抛正是最不可靠的场景），
        # 下一个真模型任务会撞上单 GPU 纪律。与主收尾同款的 SIGTERM→短等→SIGKILL。
        print((tmp / "server.log").read_text(encoding="utf-8")[-3000:], file=sys.stderr)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        raise
    return proc


def seed(corpus: list) -> None:
    for name, desc in CATEGORY_DESCRIPTIONS.items():
        requests.post(f"{BASE}/api/v1/category",
                      json={"db_name": name, "description": desc}, timeout=30).raise_for_status()
    for doc in corpus:
        requests.post(f"{BASE}/api/v1/document/insert",
                      json={"title": doc["title"], "document": doc["text"],
                            "category": doc["category"]}, timeout=60).raise_for_status()

    wanted = {d["title"] for d in corpus}

    def _all_indexed():
        seen = set()
        for cat in CATEGORY_DESCRIPTIONS:
            r = requests.get(f"{BASE}/api/v1/document/list",
                             params={"category": cat}, timeout=30).json()
            seen |= {d["title"] for d in r.get("documents", []) if d.get("chunks", 0) > 0}
        return wanted <= seen
    _wait(_all_indexed, timeout=600, interval=2, what="夹具语料全部入库")

    # 时效探针：旧住址标过时（U3 部署同款操作）
    r = requests.get(f"{BASE}/api/v1/document/list",
                     params={"category": "household"}, timeout=30).json()
    docs_listed = r.get("documents", [])
    for d in docs_listed:
        if d["title"] == "旧住址档案":
            requests.post(f"{BASE}/api/v1/document/stale",
                          json={"source_path": d["source_path"],
                                "reason": "2025-08 已搬离，现住址见「现住址与快递偏好」"},
                          timeout=30).raise_for_status()

    # 凭证指针链路冒烟（R3）：随手取一篇的 chunk 0 相邻块，端点断了当场炸而非静默
    if docs_listed:
        ctx = requests.get(f"{BASE}/api/v1/document/context",
                           params={"source_path": docs_listed[0]["source_path"],
                                   "seq": 0, "n": 1}, timeout=30).json()
        assert ctx.get("chunks"), f"取邻块冒烟失败: {ctx}"
        print(f"== 指针冒烟 ✓ {docs_listed[0]['title']} 共 {ctx.get('total_chunks')} 块")


def run_item(item: dict) -> dict:
    expect = item["expect"]
    top_k = int(expect.get("top_k", 3))
    payload = {"query": item["query"], "category": item["category"],
               "optimized_query": item["optimized_query"],
               "chunk_k_per_db": max(5, top_k * 2),   # 部署同款（rag_search 工具的放大规则）
               "rerank_top_k": top_k}
    r = requests.post(f"{BASE}/api/v1/document/query", json=payload, timeout=120)
    r.raise_for_status()
    results = r.json().get("results", [])
    titles = [(res.get("metadata") or {}).get("title", "") for res in results]

    ok, why = True, []
    first_hit_rank = None
    wanted = expect.get("hit_any") or expect.get("hit_all") or []
    for rank, t in enumerate(titles, 1):
        if t in wanted:
            first_hit_rank = rank
            break
    if "hit_any" in expect and not (set(titles) & set(expect["hit_any"])):
        ok = False
        why.append(f"hit_any 全缺 {expect['hit_any']}（实际 {titles}）")
    if "hit_all" in expect:
        missing = set(expect["hit_all"]) - set(titles)
        if missing:
            ok = False
            why.append(f"hit_all 缺 {sorted(missing)}（实际 {titles}）")
    if expect.get("require_empty") and titles:
        # 闭世界判据：库外问题**返回任何文档**都算硬凑。`miss_all` 是枚举式的（只列作者
        # 想得到的几个干扰项），在 39 篇语料里天然会漏——实测 rq-neg-004 问「宽带套餐
        # 多少钱」硬凑出《小区停车月卡续费》却因不在名单里而判过。故 negative 一律用
        # 本键，`miss_all` 退化为「即便将来放宽也绝不能出现的那几篇」的补充说明。
        ok = False
        why.append(f"库外问题应返回空，实际硬凑出 {titles}")
    if expect.get("miss_all"):
        bad = set(titles) & set(expect["miss_all"])
        if bad:
            ok = False
            why.append(f"库外问题硬凑出 {sorted(bad)}")
    if expect.get("stale_not_top1") and titles and titles[0] in expect["stale_not_top1"]:
        ok = False
        why.append(f"过时文档压过新事实排第 1: {titles[0]}")
    return {"id": item["id"], "kind": item["kind"], "pass": ok, "why": why,
            "titles": titles, "n_results": len(results),
            "mrr": (1.0 / first_hit_rank) if first_hit_rank else 0.0,
            "judged_recall": bool(wanted)}


def summarize(rows: list) -> dict:
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    out = {"overall": {"n": len(rows),
                       "pass_rate": round(sum(r["pass"] for r in rows) / len(rows), 3),
                       "mrr": round(sum(r["mrr"] for r in rows if r["judged_recall"])
                                    / max(1, sum(1 for r in rows if r["judged_recall"])), 3)},
           "by_kind": {}}
    for k, rs in sorted(by_kind.items()):
        out["by_kind"][k] = {"n": len(rs),
                             "pass_rate": round(sum(r["pass"] for r in rs) / len(rs), 3)}
    return out


# ── 棘轮（OSS 扫描 §6.4，2026-08-17）────────────────────────────────────────────
# 此前这里只 print 增减、无条件 return 0：**守卫还在，但它不再判定任何东西**
# （worldmonitor 词汇里的 Vacuous Guard）。叠加基线陈旧（stamp 20260730 的 0.941，
# 而 vl-2b 定档标定后实测已到 1.0），一次真回归会被读成「与基线持平」。
#
# 纪律（借 addyosmani 的 rank-1 棘轮）：**地板只准随检索能力改善往上抬，绝不为了让
# 一次回归通过而下调**。要下调必须是题集本身变难/换档位，且在台账里写明理由。
# 地板相对实测的下浮余量。取 0.01 而非更松的值：两次跑批的 summary 与失败题集**逐字
# 复现**，没有需要吸收的抖动；而 51 题下 0.03 恰好放行「掉一道题」（48/51=0.941 仍在
# 0.931 之上），MRR 侧同样放行一道——一次真回归可以同时躲过两条地板。真正的灵敏度
# 来自逐题对账（_row_flips），比率地板只做粗筛。
FLOOR_MARGIN = 0.01


def _current_tier() -> Optional[str]:
    """当前生产档，解析链必须与服务端**同源**：指针 → `config.yaml rag.active_tier`。

    只读指针是不够的：`.rag-tier-selection` 在 .gitignore 里（新克隆 / 另一台机器 /
    没跑过 `devctl rag-model` 的检出都没有它），而 `compute_core` 此时会回落到
    config.yaml 的 active_tier 照常起服——于是跑批本身完全有效，档位判定却报「未知」，
    棘轮走「档位不同」分支自我关闭。一个 pass 0.50 的跑批照样绿灯：**守卫被它自己
    的输入静默缩小掉**，正是本波要消灭的那类失效原样重建。
    解析不出返回 None，由调用方 fail-closed（绝不当成「跳过判定」）。
    """
    sel = REPO_ROOT / ".rag-tier-selection"
    if sel.exists():
        try:
            tier = json.loads(sel.read_text(encoding="utf-8")).get("tier")
            if tier:
                return str(tier)
        except Exception:
            pass                      # 指针坏了就往下走 config，不静默当成「没档位」
    try:
        cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
        return str(cfg["rag"]["active_tier"])
    except Exception as e:
        print(f"[评估] 档位解析失败（指针与 config 都读不到）: {e}")
        return None


def _baseline_floor(base: dict, tier: Optional[str]) -> Optional[dict]:
    """取本档位的地板。`floors` 按档位分键——单一地板会被「评一个候选档顺手 --save-baseline」
    整个覆盖掉，生产档的保护从此静默消失（此后每次生产跑批都走「档位不同」跳过）。
    兼容旧格式的单个 `floor` 字段（视为 base['tier'] 那一档的地板）。"""
    floors = base.get("floors")
    if isinstance(floors, dict):
        return floors.get(tier)
    if base.get("floor") and base.get("tier") in (None, tier):
        return base["floor"]
    return None


def _items_digest(items: list) -> str:
    """题集内容摘要：题目一改（含增删改判据），基线就必须重钉。

    只校验题数挡不住「删掉一道失败题、补一道必过题」——数量不变、`_row_flips` 也看不见
    （它只看基线里有而本次失败的 id，**从基线消失的 id 完全不可见**），于是总分被抬高、
    地板还跟着往上焊。比率型地板下题集是自由变量，摘要是把它钉住的最省事办法。
    """
    blobs = sorted(json.dumps(it, sort_keys=True, ensure_ascii=False) for it in items)
    return hashlib.sha256("\n".join(blobs).encode("utf-8")).hexdigest()[:16]


def _row_flips(base: dict, rows: list) -> tuple:
    """逐题对账，返回 (由过转败的 id, 基线有而本次缺席的 id)。

    只看总分会漏「A 退步、B 进步」这种互相抵消的回归——而抵消所需的数据本来就已经
    写在基线的 `rows` 里，此前从没有人读它。这是比总分地板更灵敏的信号。
    缺席那一半同样要报：题目被悄悄删掉时，它在这里现形而不是在总分里消失。
    """
    was = {r["id"]: r.get("pass") for r in (base.get("rows") or [])}
    now = {r["id"] for r in rows}
    return ([r["id"] for r in rows if was.get(r["id"]) is True and not r["pass"]],
            sorted(i for i in was if i not in now))


def _check_floor(base: dict, summary: dict, tier: Optional[str], disabled: bool,
                 rows: list, items: list) -> int:
    """低于地板即返回非零。**只对当前生产档强制**——评估别的档位时只打印。

    理由（对抗审查修正）：l0_eval / model_eval 那类选型工具里各档位水位天差地别，
    一个全局地板会让「评一个新候选」恒红，久而久之地板必被删——正是棘轮要防的失效。
    本评估虽是单档回归工具，同样按档位锁键，免得换档重测时误判。
    """
    if disabled:
        print("SKIP: 地板判定已被 --no-enforce-floor 关闭，只打印")
        return 0
    if tier is None:
        print("❌ 档位解析不出（指针与 config 都读不到）——fail-closed 判为不通过。"
              "绝不把「不知道跑的是哪档」当成「跳过判定」。")
        return 1

    floor = _baseline_floor(base, tier)
    if not floor:
        print(f"SKIP: 基线里没有档位 {tier} 的地板，本次只打印"
              f"（确认水位后 --save-baseline 钉上即可启用棘轮）")
        return 0

    bad_keys = [k for k in floor if k not in summary["overall"]]
    if bad_keys:
        print(f"❌ 基线地板含未知指标 {bad_keys}（可选：{sorted(summary['overall'])}）——"
              "拼错的地板键会永远红，先修基线文件。")
        return 1

    fails = []
    below = [f"{k} {summary['overall'][k]} < 地板 {v}"
             for k, v in floor.items() if summary["overall"][k] < v]
    if below:
        fails.append("低于地板：" + "；".join(below))

    n_base = base.get("n_items")
    d_base = base.get("items_digest")
    d_now = _items_digest(items)
    if n_base is not None and n_base != summary["overall"]["n"]:
        fails.append(f"题集规模变了（基线 {n_base} → 本次 {summary['overall']['n']}）——"
                     "比率型地板下题集是自由变量：加几道易题就能把总分抬过地板，"
                     "删掉失败题更是直接满分。必须重钉基线并在台账写明理由")
    elif d_base and d_base != d_now:
        fails.append(f"题集内容变了（摘要 {d_base} → {d_now}，题数没变）——"
                     "换题同样能洗白：删一道失败题、补一道必过题，题数与逐题对账都看不见。"
                     "必须重钉基线并在台账写明理由")
    else:
        flips, gone = _row_flips(base, rows)
        if flips:
            fails.append(f"逐题对账发现由过转败：{flips}（总分可能被别的题的进步抵消）")
        if gone:
            fails.append(f"基线里有、本次缺席的题：{gone}（题目被删会让总分失去可比性）")

    if fails:
        print("❌ 判为回归：\n   - " + "\n   - ".join(fails))
        print("   要么修回检索能力，要么在台账写明「题集变难/换档」的理由后重钉基线——"
              "绝不为让本次通过而下调地板。")
        return 1
    print(f"✅ 不低于地板（{tier}: {floor}），且无逐题由过转败")
    return 0


def _next_floor(base, summary: dict, tier: str, margin: float, allow_drop: bool):
    """算出要写进基线的新地板；判定为「拿回归洗白」时返回 None（调用方退非零）。

    棘轮的**核心性质是地板只增不减**。没有这一条，`--save-baseline` 就是一键洗白：
    跑出回归、顺手带上这个开关，地板反而被压低到新水位，守卫从此守在更低的地方——
    正是 addyosmani 那条「绝不为让回归通过而降低」要防的失效，也是本仓 §6.4 之所以
    发生的同型故事（基线陈旧到守不住任何东西）。
    故：
    - 实测**低于旧地板** = 这是一次回归，拒绝钉基线（除非显式 --allow-floor-drop
      并在台账写明「题集变难/换档」的理由）；
    - 实测高于旧地板但算出的新地板更低（水位小幅回落，仍在地板之上）→ 取两者较大，
      地板不动，不声不响地保住既有保护。
    """
    measured = {"pass_rate": summary["overall"]["pass_rate"], "mrr": summary["overall"]["mrr"]}
    new = {k: round(max(0.0, v - margin), 3) for k, v in measured.items()}
    old = _baseline_floor(base or {}, tier)
    if not old:
        return new

    regressed = {k: (measured[k], old[k]) for k in new if measured[k] < old.get(k, 0)}
    if regressed and not allow_drop:
        print("❌ 拒绝把一次回归钉成新基线：" +
              "；".join(f"{k} 实测 {m} < 现地板 {f}" for k, (m, f) in regressed.items()))
        print("   棘轮只准往上抬。确因题集变难/换档需要下调，加 --allow-floor-drop "
              "并在台账写明理由。")
        return None
    if allow_drop:
        return new
    kept = {k: max(v, old.get(k, 0)) for k, v in new.items()}
    if kept != new:
        print(f"== 地板保持不降：{kept}（本次算出 {new}，取较高者）")
    return kept


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save-baseline", action="store_true",
                    help="把本次结果写入 BASELINE.json（接受当前水平为基线）")
    ap.add_argument("--keep", action="store_true", help="保留临时数据根（调试）")
    ap.add_argument("--floor-margin", type=float, default=None,
                    help=f"--save-baseline 时地板相对实测的下浮余量（默认 {FLOOR_MARGIN}）")
    ap.add_argument("--no-enforce-floor", action="store_true",
                    help="只打印不判定（调试/评估非生产档时用）")
    ap.add_argument("--allow-floor-drop", action="store_true",
                    help="允许本次 --save-baseline 下调地板（仅限题集变难/换档，须带 --reason）")
    ap.add_argument("--reason", default="", help="下调地板的书面理由（写进基线做审计痕）")
    args = ap.parse_args()
    if args.floor_margin is not None and not 0.0 <= args.floor_margin <= 0.1:
        # 余量既能调松到永远绿（1.0 → 地板归零），也能调紧到自我打红（负值 → 地板高于实测）
        ap.error(f"--floor-margin 必须在 [0.0, 0.1] 内，收到 {args.floor_margin}")
    if args.allow_floor_drop and not args.reason.strip():
        ap.error("--allow-floor-drop 必须同时给 --reason：下调地板要留得下审计痕")

    corpus = _load_jsonl(HERE / "corpus.jsonl")
    items = _load_jsonl(HERE / "items.jsonl")
    tmp = Path(tempfile.mkdtemp(prefix="rag-eval-"))
    print(f"== 隔离数据根 {tmp}，拉起融合模式评估实例 :{PORT} …")
    try:
        proc = start_server(tmp)
    except Exception:
        # 起服失败时下面的 finally 还没进，临时数据根会留在 /var/folders 里（含权重缓存
        # 与 chroma 目录，不小）。进程侧由 start_server 自己收；这里收盘。
        if not args.keep:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        raise
    try:
        print(f"== 灌入夹具语料 {len(corpus)} 篇 …")
        seed(corpus)
        print(f"== 跑题 {len(items)} 条 …")
        rows = [run_item(it) for it in items]
    finally:
        # 收尾必须稳健：真实推理后服务端（双模型 CUDA 上下文 + chroma 持久化）在
        # SIGTERM 下可能 >30s 才退——旧版 proc.wait(timeout=30) 直接抛
        # TimeoutExpired，把已算完的 rows 连同 summarize/落盘一起带走（2026-08-08
        # RAG 五档连评全失败之真相：评测其实跑完了，死在收尾）。SIGTERM→短等→
        # SIGKILL 兜底，收尾异常一律吞掉不影响结果保存。
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        if not args.keep:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    summary = summarize(rows)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = HERE / "results"
    outdir.mkdir(exist_ok=True)
    (outdir / f"{stamp}.json").write_text(
        json.dumps({"stamp": stamp, "summary": summary, "rows": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    for r in rows:
        if not r["pass"]:
            print(f"❌ {r['id']} [{r['kind']}] " + "; ".join(r["why"])[:180])
    print("== 总览:", json.dumps(summary["overall"], ensure_ascii=False))
    print("== 分形态:", json.dumps(summary["by_kind"], ensure_ascii=False))

    baseline_path = HERE / "BASELINE.json"
    tier = _current_tier()
    base = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else None
    exit_code = 0
    print(f"== 本次档位: {tier or '解析失败'}")
    if base:
        b = base["summary"]
        print(f"== 对比基线（{base.get('stamp')}）: "
              f"pass {b['overall']['pass_rate']} → {summary['overall']['pass_rate']}  "
              f"MRR {b['overall']['mrr']} → {summary['overall']['mrr']}")
        for k, v in summary["by_kind"].items():
            bv = (b["by_kind"].get(k) or {}).get("pass_rate")
            if bv is not None and v["pass_rate"] != bv:
                print(f"   {k}: {bv} → {v['pass_rate']}")
        exit_code = _check_floor(base, summary, tier, args.no_enforce_floor, rows, items)
    elif args.save_baseline:
        print("SKIP: 无基线文件，本次自举钉基线")
    else:
        # 与 tier 解析不出同样 fail-closed：删掉基线文件是唯一一条「删个文件就关掉守卫
        # 且退 0」的路径，而脚本自陈要防的正是「久而久之地板必被删」。自举请显式带
        # --save-baseline（基线入库，误删会在 diff 里现形）。
        print("❌ 无基线文件且未指定 --save-baseline——判为不通过（自举请显式钉基线）")
        return 1

    if args.save_baseline:
        if tier is None:
            print("❌ 档位解析不出，拒绝钉基线（否则地板会挂到一个不知道是什么的档上）")
            return 1
        # 已判回归却顺手 --save-baseline = 一键洗白。地板那一维由 _next_floor 拦，但
        # 逐题由过转败与题集变更两维原先无人设防：打印了 ❌ 仍退 0，且基线 rows 被覆盖，
        # 下次再也对不出这次的由过转败（复审 M1）。故此处统一要求显式放行。
        if exit_code != 0 and not args.allow_floor_drop:
            print("❌ 本次已判为回归，拒绝把它钉成新基线。确属题集变难/换档，"
                  "请显式 --allow-floor-drop --reason \"…\"。")
            return exit_code
        # 余量沿用基线里记的那个：否则一次刻意收紧过的余量，会在下次重钉时静默弹回默认值。
        # 判据用 sentinel 而非扫 sys.argv——argparse 允许前缀缩写（`--floor-marg 0.05`
        # 能解析成功却被 startswith 探测漏掉，于是静默丢弃显式入参还打印一句假话）。
        margin = args.floor_margin
        if margin is None:
            margin = float(base["floor_margin"]) if base and "floor_margin" in base \
                else FLOOR_MARGIN
            if base and "floor_margin" in base:
                print(f"== 沿用基线记录的地板余量 {margin}（未显式指定 --floor-margin）")
        floor = _next_floor(base, summary, tier, margin, args.allow_floor_drop)
        if floor is None:
            return 1             # 拿回归当新基线 = 一键洗白，拒绝（见 _next_floor）
        floors = dict((base or {}).get("floors") or {})
        if not floors and (base or {}).get("floor") and (base or {}).get("tier"):
            floors[base["tier"]] = base["floor"]      # 旧格式迁移：单地板归到它自己那档
        floors[tier] = floor                          # 只动本档，别的档位的地板原样留着
        payload = {"stamp": stamp, "tier": tier, "floors": floors,
                   "floor_margin": margin, "n_items": len(rows),
                   "items_digest": _items_digest(items),
                   "summary": summary,
                   "rows": [{k: r[k] for k in ("id", "kind", "pass")} for r in rows]}
        if args.allow_floor_drop:
            payload["floor_drop_reason"] = args.reason   # 下调地板必须留审计痕
        baseline_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
        print(f"== 基线已更新: {baseline_path}（档位 {tier}，地板 {floor}；"
              f"其余档位地板保留 {sorted(set(floors) - {tier}) or '无'}）")
        exit_code = 0            # 刚钉的基线不自我判定
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
