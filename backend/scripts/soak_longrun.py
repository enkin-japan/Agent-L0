"""本机长稳测试工装（26b-4bit 长稳门禁，2026-08-18；口径 8h，见 LONGRUN_HOURS）。

**为什么是重写**：2026-08-08 那次 26B 12h 长稳的工装只活在 Colab VM 上、从未入库，
随实例销毁（全分支 git 历史与 Drive 双向搜索均无）。台账只留下结论与负载形态描述。
本文件按那份描述在本机 MLX 栈上重建并入库——下次不必再写第三遍。

判据沿用上次已验证的三条（台账「26B 12h 长稳测试站 PASS」）：
  无泄漏：进程 RSS / peak_memory / 系统 wired 与 compressed 起步一档上抬后走平；
          收官在册缓存数回到预期、active_generations 归零；
  无漂移：把全程切三段比 tps / TTFT / 时长——**单调劣化才是漂移，方差不是**；
  无错误：errors_total 增量为零、健康探针全绿、取消双路清理干净。

**本工装的第一原则：宁可不测，也不假装测到。**
初版（首过评审 6 Critical）的教训全在这里——工具腿没渲染工具声明，约束采样一步没跑
却报「工具轮全过」；`constrained_steps` 只在 done 事件里而客户端丢弃了 done；RSS 用
`lsof -ti :port` 取 PID 会抓到工装自己，「无泄漏」建立在一条根本不是服务端的平线上。
故凡是判据依赖的量，这里都**要么测到并断言、要么响亮记为失效**，不留静默空转。
v2 首过评审（5 Critical / 15 Major）补掉的是同一类的**判据侧**失效——判据本身写得
不可能失败：泄漏阈值拿只增不减的累计数当上限；末态 `/metrics` 读不到时静默判「无泄漏」
再退 0；采样量取不到时全 null 而「采样健康」全绿；「reused>0」在「每轮全量重渲染」
的退化态下同样是 100%（它防不住它唯一要防的那件事）；取消腿把服务端 error 事件记成
「生成先自然结束」的通过。判据写得不可能失败，等于没有判据。

**起跑自检**（`_preflight`）：把整场时长押上去之前先证明该测的都测得到——鉴权、服务端
模型身份、三个系统采样量、四条腿各跑一次且关键仪表真出数。任一不过即拒绝起跑。
没有它，一个配置层面的失效（比如约束采样被服务端关掉）会烧满整场才在 summary
里看见。自检产物走 `preflight.jsonl`，不混进 `req.jsonl` 的统计口径。

与 Colab 那次的口径差异（换平台必须换的）：Apple Silicon 没有 VRAM，统一内存的等价
观测量是 peak_memory（MLX 进程高水位）+ 进程 RSS + 系统 wired + **compressor 页数与
swapouts**——后者才是 MLX 超售换页崩塌的先兆（单 GPU 纪律里那条 page-thrash cliff）。

用法（backend venv，服务须已就绪）：
  cd backend && PYTHONUNBUFFERED=1 .venv/bin/python scripts/soak_longrun.py \
      --outdir ../benchmarks/data/soak-26b4bit-<日期>        # 缺省 8h，见 LONGRUN_HOURS
密钥只走 `INFER_API_KEY` 环境变量或 `.env.local`——**刻意不提供 --api-key**，命令行参数
会把钥匙暴露在 `ps` 输出里长达整场。

产物：`req.jsonl`（每请求一行）/ `sys.jsonl`（系统采样）/ `preflight.jsonl`（起跑自检）/
`profile.json`（起跑即落：模型、是否冒烟、负载档位——长跑被 SIGKILL 时 summary 不会
写，没有它两个 jsonl 再也无法自证身份）/ `summary.json`（收官判据）。

退出码是**运行态**信号，不是质量判决：0=跑完且无错、无泄漏、判据齐全 /
1=有错误或缓存泄漏或**判据不可用**（末态读不到、采样残缺、服务中途换过进程——
这些一律不许静默走成绿灯）/ 2=连续失败熔断提前中止 / 3=主循环异常中止（收官照常写）/
4=收到 SIGINT/SIGTERM 提前中止（**不与「跑到时限」同码**，否则一次被 kill 的
门禁会带着 exit 0 进台账）。
漂移的 PASS/FAIL 要人读 summary 的三分段表（见上「无漂移」）；三段按**墙钟**切，
`mem_三段_静默态` 才是内存判据（生成在飞、或刚跑完几秒内，wired 会瞬时抬高一个量级）。
"""
import argparse
import bisect
import json
import re
import os
import random
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_baseline import (BACKEND_DIR, REPO_ROOT, Client,  # noqa: E402
                            build_user_prompt, make_dialect, model_options,
                            read_env_key)
from framework.core import prompt_stream as pstream  # noqa: E402

REPO = "mlx-community/gemma-4-26b-a4b-it-4bit"

# 负载权重按上次 313 请求的实际构成归一（chat 220 / tool 65 / longchain 19 / cancel 9），
# 保持同一口径便于跨次比对。
MIX = [("chat", 0.70), ("tool", 0.21), ("longchain", 0.06), ("cancel", 0.03)]

IDLE_SHORT = (20, 90)            # 秒
IDLE_LONG = (15 * 60, 45 * 60)   # 「离开」窗：磨 TTL 回收与冷启动重建
LONG_IDLE_EVERY = 40
SAMPLE_INTERVAL = 300.0          # sys.jsonl 采样间隔
HEARTBEAT_INTERVAL = 300.0       # 心跳按**墙钟**——按请求数计会在长空闲窗里静默一小时，
                                 # 把 3 分钟级的 task-sentinel 骗成停滞告警
MAX_CONSEC_FAIL = 5              # 连续失败熔断：服务死了不该空转到收官
CHAT_TURNS = 5                   # 一段会话的轮数（多轮才吃得到前缀复用）
SMOKE_MAX_HOURS = 1.0            # --smoke 的时长上限：等权+秒级空闲是高压档，
                                 # 忘带 --hours 就会拿缺省时长跑一整夜的高压跑
# 长稳口径：12h → 8h（用户 2026-08-18 拍板）。理由是形态而非妥协——本系统很长一段时间
# 只自用、不商用，**实战使用本身就是持续的长稳测试**，专门的门禁只需覆盖「一个完整工作日
# 的连续使用」这个尺度；再往上的时长交给日常使用去磨，比再烧 4 小时 GPU 划算。
LONGRUN_HOURS = 8.0


def _server_cache_ttl_s() -> float:
    """服务端缓存 TTL。收官泄漏判据的期望值全靠它——硬编码必然与 config 漂移，
    读 `inference/server.py` 用的同一个键，读不到才回落到它的同一个缺省值。"""
    try:
        import yaml
        cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        v = ((cfg.get("inference") or {}).get("remote") or {}).get("cache_ttl_minutes")
        if v:
            return float(v) * 60.0
    except Exception:
        pass
    return 45 * 60.0


def _apply_smoke_profile() -> None:
    """--smoke：把负载改成「四腿等权 + 秒级空闲」，只为验仪表真出数，不产长稳判决。

    为什么必须改权重：按生产权重跑 20 分钟约 20-25 个请求，cancel 腿（3%）的期望不到
    一次——冒烟大概率零样本通过，「四腿全绿」于是建立在一条根本没跑过的腿上，正是本
    工装第一原则要堵的那类假象。等权后每腿约 10+ 次，`constrained_steps`、`reused`、
    取消清理这三个判据才有数据支撑。
    长空闲窗要磨的是 45min 缓存 TTL，短程磨不到，故退化为短空闲（不是「跳过检查」，
    是这项本来就不在短程的射程内——summary 的 smoke 标记会把这点带进产物）。
    """
    global MIX, IDLE_SHORT, IDLE_LONG, LONG_IDLE_EVERY, QUIET_AFTER_REQ_S
    global SAMPLE_INTERVAL, HEARTBEAT_INTERVAL
    MIX = [("chat", 0.25), ("tool", 0.25), ("longchain", 0.25), ("cancel", 0.25)]
    # 空闲窗**必须长于** QUIET_AFTER_REQ_S，否则采样几乎永远落在「上一请求刚跑完」的
    # 窗口里、静默态行数恒为个位数，`mem_三段_静默态` 那条阻断就成了**恒不可过**的判据
    # ——冒烟是每次长稳前必跑的一关，一条永远失败的阻断会训练操作者无视「判据阻断」，
    # 与本工装第一原则背道而驰。实测（旧档 2-8s）：三份冒烟静默行只有 1/2/4 行。
    IDLE_SHORT = (10, 25)
    IDLE_LONG = IDLE_SHORT
    LONG_IDLE_EVERY = 10 ** 9
    SAMPLE_INTERVAL = 60.0
    HEARTBEAT_INTERVAL = 60.0
    # 短程档把静默门槛从 5s 放到 3s：实测被污染的采样都落在请求结束后 0.08–0.9s 内，
    # 3s 仍有三倍余量，而 20 分钟只有 21 个采样点，5s 会把静默样本压到不足以分段。
    # 这个值随产物落盘（summary 的 `口径`），不是暗改。
    QUIET_AFTER_REQ_S = 3.0


TOPICS = ["周末露营装备", "阳台种菜浇水", "旧笔记本装 Linux", "咖啡手冲参数",
          "地铁通勤路线", "冰箱除霜", "自行车链条保养", "阳台防水补漆"]
FOLLOWUPS = ["那预算大概多少", "有没有更省事的做法", "第一步先做什么",
             "冬天需要注意什么吗", "这个能自己动手吗"]

TOOL_SPEC = {
    "name": "lookup_note",
    "description": "Look up a personal note by keyword.",
    "parameters": {"type": "object",
                   "properties": {"keyword": {"type": "string"},
                                  "limit": {"type": "integer"}},
                   "required": ["keyword"]},
}


def _page_size() -> int:
    """从 vm_stat 首行读页大小，不硬编码 16384。"""
    try:
        head = subprocess.run(["vm_stat"], capture_output=True, text=True,
                              timeout=10).stdout.splitlines()[0]
        for tok in head.replace(",", " ").split():
            if tok.isdigit() and int(tok) >= 4096:
                return int(tok)
    except Exception:
        pass
    return 16384


def _vm_stat(page: int) -> dict:
    """系统内存态：wired / compressor / swap。这是本机版的「VRAM 曲线」。"""
    out = {}
    try:
        r = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10)
        for line in (r.stdout or "").splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            v = v.strip().rstrip(".")
            if not v.isdigit():
                continue
            k = k.strip()
            if k == "Pages wired down":
                out["wired_mb"] = int(v) * page // (1 << 20)
            elif k == "Pages occupied by compressor":
                out["compressed_mb"] = int(v) * page // (1 << 20)
            elif k == "Swapins":
                out["swapins"] = int(v)
            elif k == "Swapouts":
                out["swapouts"] = int(v)
    except Exception as e:
        out["vm_stat_error"] = str(e)[:80]
    return out


def _listener_pid(port: int) -> "str | None":
    """服务端 PID。**必须带 -sTCP:LISTEN**：裸 `lsof -ti :PORT` 同时匹配本地与远端端口，
    工装自己那条长连接照样命中，而 pids[0] 取的是枚举顺序不是监听者——实测两进程探针
    `lsof -ti :P -> [服务端, 客户端]`。取错就会采到工装自己的 RSS，得到一条完美平线。"""
    try:
        pids = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                              capture_output=True, text=True, timeout=10).stdout.split()
        return pids[0] if pids else None
    except Exception:
        return None


def _rss_mb(pid: "str | None") -> "int | None":
    if not pid:
        return None
    try:
        r = subprocess.run(["ps", "-o", "rss=", "-p", pid],
                           capture_output=True, text=True, timeout=10)
        return int(r.stdout.strip()) // 1024
    except Exception:
        return None


def _peak_max(rows: list) -> "float | None":
    vals = [r["peak_mem"] for r in rows if isinstance(r.get("peak_mem"), (int, float))]
    return max(vals) if vals else None


def _peak_last(rows: list) -> "float | None":
    for r in reversed(rows):
        if isinstance(r.get("peak_mem"), (int, float)):
            return r["peak_mem"]
    return None


def _peak_steps(rows: list) -> int:
    """peak_memory 抬升次数，**不含首次建立**（第一条请求把高水位从 0 抬到 17.5GB
    不是「抬升」，把它算进去会让「8 次抬升」这种叙述比实际多一次）。"""
    prev, n = 0.0, -1
    for r in rows:
        pm = r.get("peak_mem")
        if isinstance(pm, (int, float)) and pm > prev + 1e-9:
            n += 1
            prev = pm
    return max(n, 0)


def _first_last(rows: list, key: str) -> list:
    """取**含该键**的首末行的值。首末行若恰好是 sampler_error 行，直接下标会得到
    两个 null，而看的人无从知道是「没换页」还是「没采到」。"""
    vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return [vals[0], vals[-1]] if vals else [None, None]


def _peak_step(rows: list) -> "float | None":
    """peak_memory 的「首小时之后还涨了多少」。高水位量做分段平均恒单调，只有台阶
    有意义：起步一档上抬是正常的（缓存建起来），首小时之后再上台阶才是泄漏征兆。"""
    first = [r["peak_mem"] for r in rows
             if isinstance(r.get("peak_mem"), (int, float))
             and isinstance(r.get("t"), (int, float)) and r["t"] <= 3600]
    later = [r["peak_mem"] for r in rows
             if isinstance(r.get("peak_mem"), (int, float))
             and isinstance(r.get("t"), (int, float)) and r["t"] > 3600]
    if not first or not later:
        return None
    return round(max(later) - max(first), 4)


QUIET_AFTER_REQ_S = 5.0   # 见 _quiet_rows

def _quiet_rows(sysrows: list, rows: list, quiet_s: float = None) -> list:
    """内存判据可用的采样行：既要「采样瞬间没有生成在飞」，**还要离上一次请求结束
    足够久**。

    只看 `active_generations == 0` 是不够的——wired 的回落有**秒级滞后**，而
    active_generations 在生成一结束就归零。实测本次跑：4 行「空闲态」样本的 wired 是
    18358–19220MB，而真实空闲基线是 3066–3327MB，这 4 行的共同点全是「上一条请求在
    0.1–0.9 秒前刚刚结束」。混进去之后，三段均值就变成「每段碰巧摊到几个刚跑完的样本」
    的函数，而不是内存趋势——smoke3 的同一张表因此读出过 8981/6184/2824 的假单调下降。
    """
    ends = sorted(r["t"] for r in rows if isinstance(r.get("t"), (int, float)))
    out = []
    for r in sysrows:
        mt = r.get("metrics")
        if not (isinstance(mt, dict) and mt.get("active_generations") == 0):
            continue
        if not isinstance(r.get("server_rss_mb"), (int, float)):
            continue
        i = bisect.bisect_right(ends, r["t"])
        # **没有已知的前一次请求 ≠ 静默**，而是「无从判定」，必须排除。
        # `t0 基线` 那一行正是这个坑：它写在起跑自检（含一次 768 token 长链）刚跑完
        # 之后、t≈0.1，req.jsonl 里没有任何更早的请求，于是「prev 为空就放行」会把
        # **全文件污染最重的一行**当成静默态——实测它 wired=18511 而真实静默基线是
        # 3066–3327，单这一行就把干净窗第一段从 3086.9 抬到 3568.9，凭空造出
        # 「第一段高、后面走平」的假象，正是本过滤器要杀的那个形态。
        if i == 0 or r["t"] - ends[i - 1] < (QUIET_AFTER_REQ_S if quiet_s is None
                                              else quiet_s):
            continue
        out.append(r)
    return out


def measure(rows: list, sysrows: list, t_end: float, spec: dict = None):
    """把「从原始 jsonl 算出判据」这件事做成**纯函数**，收官与事后重算共用同一份实现。

    为什么必须是纯函数：本站曾把「干净窗口径」的三段数写进台账与 commit，而那组数出自
    一次未入库的临时分析——换个人重算，第三段边界含不含某一行、n≥5 规则、剔不剔
    `cache_rotated`，任一取法不同就是另一组数。判据必须能被别人用同一段代码复现出来。
    `--reanalyze` 走的就是这里。

    `spec` 是本次判据的**口径**（`mix` / `sample_interval` / `quiet_after_req_s`），
    必须由调用方传入而不是读模块全局——`_apply_smoke_profile()` 会在运行期改写那几个
    全局量，而 `--reanalyze` 不会调用它：读全局就会给一份冒烟产物盖上生产权重的
    `口径`，**记下一份假的出处比不记更糟**（对着它核 `by_kind` 会得出「负载配错了」）。
    返回 `(判据 dict, 有效采样行)`——有效行不再夹带在 dict 里让每个调用方去 pop。
    """
    spec = spec or {}
    mix = spec.get("mix") or MIX
    sample_interval = spec.get("sample_interval") or SAMPLE_INTERVAL
    quiet_s = spec.get("quiet_after_req_s")
    quiet_s = QUIET_AFTER_REQ_S if quiet_s is None else quiet_s
    def seg(rs, i):
        lo, hi = t_end * i / 3, t_end * (i + 1) / 3
        return [r for r in rs
                if isinstance(r.get("t"), (int, float))
                and (lo <= r["t"] < hi or (i == 2 and r["t"] >= lo))]

    def avg(rs, k):
        v = [r[k] for r in rs if isinstance(r.get(k), (int, float))]
        if not v:
            return None
        # ttft/wall 是亚秒量，3 位小数会把 0.3692/0.3774 压成 0.369/0.377——引用方
        # 一旦写了 4 位就再也复现不出来（台账里正好有这么一处）。
        return round(sum(v) / len(v), 4 if k in ("ttft", "wall") else 3)

    def drift(pred, keys):
        """按同质分组做三分段——四类负载混在一起平均，段间构成的随机波动会被
        读成漂移（或把真漂移稀释掉）。n<5 的格子标 null，不给不可用的数。"""
        sel = [r for r in rows if pred(r)]
        out = {}
        for k in keys:
            cells, ns = [], []
            for i in range(3):
                sg = seg(sel, i)
                ns.append(len(sg))
                cells.append(avg(sg, k) if len(sg) >= 5 else None)
            out[k] = {"三段": cells, "n": ns}
        return out

    # 采样有效性按**含 server_rss_mb 的行**算，不按行数：`_listener_pid`/`_rss_mb`
    # 失败时返回 None 而不抛，行照写、sampler_died 照样是 None，于是「采样健康」
    # 全绿而 mem_三段 是 [null,null,null]。sampler_error 行同理不能算进分子。
    valid_sys = [r for r in sysrows if isinstance(r.get("server_rss_mb"), (int, float))]
    quiet_sys = _quiet_rows(sysrows, rows, quiet_s)
    # 冷轮（缓存被 TTL 回收后的重建）要从「turn2+ 热」里剔除：一次冷预填的 TTFT
    # 比热轮高一个量级，随机落段足以在均值上造出假的单调劣化。
    def _hot(r):
        return (r.get("kind") == "chat" and (r.get("turn") or 0) >= 2
                and not r.get("cache_rotated"))
    chat2 = [r for r in rows if _hot(r)]
    has_exp = [r for r in chat2 if isinstance(r.get("reuse_expect"), int)]
    cancels = [r for r in rows if r.get("kind") == "cancel"]
    tools = [r for r in rows if r.get("kind") == "tool"]
    return {
        "窗口秒": round(t_end, 1),
        "requests": len(rows),
        "errors": sum(1 for r in rows if not r.get("ok")),
        "by_kind": {k: sum(1 for r in rows if r.get("kind") == k) for k, _ in mix},
        "drift_三段": {
            "chat_turn1_冷": drift(lambda r: r.get("kind") == "chat" and r.get("turn") == 1,
                                   ("ttft", "tps", "wall")),
            "chat_turn2+_热": drift(_hot, ("ttft", "tps", "wall")),
            "tool": drift(lambda r: r.get("kind") == "tool", ("ttft", "tps")),
            "longchain": drift(lambda r: r.get("kind") == "longchain", ("ttft", "tps")),
        },
        # 内存三段只用**静默态**采样行（无生成在飞，且离上一请求结束 ≥5s）：wired 在有
        # 生成在飞或刚跑完时会瞬时抬高一个量级，混采下的三段均值取决于「碰巧采到几个」。
        "mem_三段_静默态": {k: [avg(seg(quiet_sys, i), k) for i in range(3)]
                         for k in ("server_rss_mb", "wired_mb", "compressed_mb")},
        # 每段 n 必须给：`mem_三段` 没有 drift 那条 n≥5 规则，静默行若挤在某一段，
        # 表会读成 [3086, null, null] 而总数阈值照样放行——内存判据于是只覆盖三分之一。
        "mem_三段_静默态_n": {k: [sum(1 for r in seg(quiet_sys, i)
                                     if isinstance(r.get(k), (int, float)))
                                 for i in range(3)]
                             for k in ("server_rss_mb", "wired_mb", "compressed_mb")},
        "mem_三段_全部": {k: [avg(seg(valid_sys, i), k) for i in range(3)]
                       for k in ("server_rss_mb", "wired_mb", "compressed_mb")},
        "静默态行数": {"静默": len(quiet_sys), "有效": len(valid_sys), "总": len(sysrows)},
        # server_rss_mb 是整数 MB，三段均值恰好相等常常只是 ±1 对消的巧合，故一并给出
        # 原始取值跨度——「一动不动」这种话要有原始分布撑着才敢说。跨度必须按**静默态**
        # 口径算：拿全量行的跨度去佐证静默态的三段，是在用另一个总体说话。
        "rss_跨度_mb_静默态": ([min(r["server_rss_mb"] for r in quiet_sys),
                            max(r["server_rss_mb"] for r in quiet_sys)] if quiet_sys else None),
        "rss_跨度_mb_全部": ([min(r["server_rss_mb"] for r in valid_sys),
                          max(r["server_rss_mb"] for r in valid_sys)] if valid_sys else None),
        "wired_跨度_mb_静默态": ([min(r["wired_mb"] for r in quiet_sys
                                 if isinstance(r.get("wired_mb"), (int, float))),
                             max(r["wired_mb"] for r in quiet_sys
                                 if isinstance(r.get("wired_mb"), (int, float)))]
                            if any(isinstance(r.get("wired_mb"), (int, float))
                                   for r in quiet_sys) else None),
        # 口径常量随产物走：不记下来，后人就还是只能靠钉住 commit 才复现得出这张表
        # ——那正是 M6 那条缺陷换个层级复发。
        "口径": {"QUIET_AFTER_REQ_S": quiet_s, "MIX": mix,
               "SAMPLE_INTERVAL": sample_interval,
               "来源": spec.get("来源") or "运行期模块常量"},
        # peak_memory 是 MLX 的**进程级高水位**，永不复位，做分段平均必然单调上升。
        # max 与末值要分开报：服务若中途重启过，高水位会归零重来。
        "peak_mem_高水位": {"max": _peak_max(rows), "末值": _peak_last(rows),
                         "首小时后增量": _peak_step(rows),
                         "台阶次数(不含首次建立)": _peak_steps(rows)},
        "swap_首末": _first_last(sysrows, "swapouts"),
        # 「reused>0 占比」证明不了任何事（每轮全量重渲染 + 同 cache_id 时同样是
        # 100%）。真正的判据是逐行硬断言，这里报的是它的通过率与失败明细。
        "前缀复用": {
            "turn≥2 非冷轮样本": len(chat2),
            "增量组装占比": (round(sum(1 for r in chat2 if r.get("assembly") == "delta")
                              / len(chat2), 3) if chat2 else None),
            "token级喂入占比": (round(sum(1 for r in chat2 if r.get("delta_feed"))
                                / len(chat2), 3) if chat2 else None),
            # 分母必须剔掉 reuse_expect 为空的行：`0 >= (None or 0) - 2` 恒真，
            # 上一轮生成挂掉的会话会让这条占比在退化最严重时读出 100%。
            "命中达期望占比": (round(sum(1 for r in has_exp
                                   if (r.get("reused") or 0) >= r["reuse_expect"] - 2)
                                / len(has_exp), 3) if has_exp else None),
            "无期望值行数": len(chat2) - len(has_exp),
            "失败明细": [r.get("error") for r in chat2 if not r.get("ok")][:10],
        },
        "工具腿": {"样本": len(tools),
                 "发出调用占比": (round(sum(1 for r in tools if r.get("called"))
                                / len(tools), 3) if tools else None)},
        "取消腿": {"总数": len(cancels),
                 "按路": {v: sum(1 for r in cancels if r.get("via") == v)
                         for v in ("endpoint", "disconnect")},
                 "生成先自然结束": sum(1 for r in cancels if r.get("skipped")),
                 "取消到达时已结束(竞态)": sum(1 for r in cancels if r.get("race")),
                 "未测到(信号中止)": sum(1 for r in cancels if r.get("settle_aborted")),
                 "失败": sum(1 for r in cancels if not r.get("ok"))},
        "健康探针": {"失败次数": sum(1 for r in sysrows if r.get("health_error")),
                  "最差 ms": (max((r["health_ms"] for r in sysrows
                                  if isinstance(r.get("health_ms"), (int, float))), default=None))},
    }, valid_sys


class Soak:
    def __init__(self, args, dialect, api_key: str):
        self.args = args
        self.dialect = dialect
        self.client = Client(args.port, api_key)   # 钥匙只活在 Client 里，不上 args
        self.out = Path(args.outdir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.req_f = (self.out / "req.jsonl").open("a", encoding="utf-8")
        self.sys_f = (self.out / "sys.jsonl").open("a", encoding="utf-8")
        self.rng = random.Random(args.seed)
        self.page = _page_size()
        self.t0 = time.time()
        self.deadline = self.t0 + args.hours * 3600
        self.n = 0
        self.errors = 0
        self.consec_fail = 0
        self.samples = 0
        self.sampler_died = None
        self.del_attempt = 0
        self.del_ok = 0
        # 交给 TTL 的缓存**逐份记时刻**，不是记个累计数：累计数只增不减，长跑下会涨到
        # 几十上百，而末态真正还在册的只有最后一个 TTL 窗内交出去的那几份。拿累计数当
        # 阈值，等于把「泄漏 70 份以下」全部放行——三条头等判据之一结构性失灵。
        self.ttl_marks: list = []
        self.retired: set = set()     # 已退休的 cache_id：异常路径上 _inflight 与
                                      # sess["cache_id"] 常是同一份，重复退休会既虚增
                                      # 删除腿的「发起-成功」差、又虚增 TTL 容差
        self.cache_ttl_s = _server_cache_ttl_s()
        self.stop = threading.Event()
        self.sampler_thread = None
        self.base_metrics = {}
        self.preflight = {}           # 起跑自检结论，进 summary
        self.verdict_blockers = []    # 判据不可用的响亮记录（非空即退非零）
        # 在途缓存登记处：超时/EOF/error 这些异常路径上，cache_id 会随异常一起丢，
        # 那份 26B 的 KV 就泄到服务端 TTL 收摊为止——长跑里每次腰斩漏一份，既堆内存，
        # 又会被工装自己的收官判据算成服务端泄漏（自己制造的假阳）。
        self._inflight = {}

    def _close_files(self) -> None:
        for f in (self.req_f, self.sys_f):
            try:
                f.close()
            except Exception:
                pass

    # ---------- 落盘（逐行 flush：进程被杀只丢最后一行）----------
    def _write(self, f, rec: dict) -> None:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()

    def _req(self, **kw) -> None:
        self.n += 1
        self._write(self.req_f, {"i": self.n, "t": round(time.time() - self.t0, 2), **kw})
        if kw.get("ok", True):
            self.consec_fail = 0
        else:
            self.errors += 1
            self.consec_fail += 1

    # ---------- 系统采样线程 ----------
    def _sample_once(self) -> dict:
        pid = _listener_pid(self.args.port)
        rec = {"t": round(time.time() - self.t0, 1), **_vm_stat(self.page),
               "server_pid": pid, "server_rss_mb": _rss_mb(pid)}
        t = time.time()
        try:
            self.client.json_call("GET", "/health", timeout=10)
            rec["health_ms"] = round((time.time() - t) * 1000, 1)
        except Exception as e:
            rec["health_error"] = str(e)[:120]
        try:
            rec["metrics"] = self.client.json_call("GET", "/metrics", timeout=10)
        except Exception as e:
            rec["metrics_error"] = str(e)[:120]
        return rec

    def sampler(self) -> None:
        # 整个循环体套 try：采样线程静默死亡 = 内存曲线断掉 = 「无泄漏」这条结论没了，
        # 而 summary 照样会拿前两小时的数据算出三段来，看起来毫无异常。
        while not self.stop.wait(SAMPLE_INTERVAL):
            try:
                self._write(self.sys_f, self._sample_once())
                self.samples += 1
            except Exception as e:
                self.sampler_died = f"{type(e).__name__}: {str(e)[:120]}"
                try:
                    self._write(self.sys_f, {"t": round(time.time() - self.t0, 1),
                                             "sampler_error": self.sampler_died})
                except Exception:
                    pass       # 连错误都写不下去（盘满）：留给 finish() 的行数比对暴露

    def _cout(self) -> dict:
        """交给 Client.generate 的在途登记处（每次请求前清空、主循环异常路径按它退休）。"""
        self._inflight.clear()
        return self._inflight

    # ---------- 四类负载 ----------
    def _chat_turn(self, sess, history):
        """短聊多轮：**镜像引擎的流式组装**（上位解口径）——turn1 全量渲染，turn2+ 走
        「记录流 + 方言增量段」+ token 级增量喂入。

        绝不能用「每轮重渲染整段历史」：实测那样得到的 turn2 prompt **不以 turn1 的
        prompt+生成为前缀**（env 锚点块每轮后移、assistant 回放带思考频道前缀，正是
        上位解修掉的两个破坏者），前缀复用一次都不会命中——chat 腿会静默退化成
        「每轮冷预填」，而 KV 增长与复用这两条恰恰是长稳最该盯的。
        与 bench_a8_multiturn v3 同源。
        """
        history.append({"role": "user",
                        "content": self.rng.choice(FOLLOWUPS) if history
                        else f"帮我想想{self.rng.choice(TOPICS)}要准备什么"})
        ps, cache_id = sess["ps"], sess["cache_id"]
        used_delta, tail = False, None
        if ps is not None and pstream.stream_valid(ps, history, None):
            d_out = self.dialect.build_prompt_delta(history[ps.seen + 1:], prev_tail=ps.tail)
            if d_out is not None:
                prompt, tail = ps.stream + d_out[0], d_out[1]
                used_delta = True
        if not used_delta:
            prompt = self.dialect.build_prompt(messages=history, tools=None,
                                               include_env=False)
            tail = self.dialect.tail_state()
        done, raw, cout = {}, {}, self._cout()
        last, text, ttft, cid, wall = self.client.generate(
            prompt, cache_id=cache_id, sampling={"max_tokens": 160, "temperature": 0.7},
            features=({"prompt_delta_offset": len(ps.stream)} if used_delta else None),
            done_out=done, raw_out=raw, cache_out=cout)
        # 次序不可颠倒：commit 记下 seen=len(history)，并**预期下一轮 history[seen]
        # 正是这条 assistant**——先 append 再 commit 会让 seen 多算一位，
        # stream_valid 从此恒假、增量路永不生效（chat 腿静默退化成每轮冷预填）。
        # 提交用**原始字节口径**（raw_out）：展示 delta 与引擎生成字节不逐字等价。
        # 拿不到原始字节就**不提交**（退全量重来），而不是拿展示口径顶上——那会让
        # ps.stream 与服务端记录逐字不符，下一轮的「复用命中」是错的却依然是绿的。
        raw_text = raw.get("text")
        sess["ps"] = (pstream.commit(prompt, raw_text, history, None, tail)
                      if raw_text else None)
        sess["uncommitted"] = not raw_text
        history.append({"role": "assistant", "content": text})
        sess["cache_id"] = cid or cout.get("cache_id")   # 与返回值同口径，别让会话丢句柄
        gen_tok = (last or {}).get("generation_tokens") or 0
        prompt_tok = (last or {}).get("prompt_tokens")
        turn = len(history) // 2
        extra = (last or {}).get("extra") or {}
        reused = extra.get("reused_prefix")
        rotated = bool(cache_id and cid != cache_id)
        prev_total = sess.get("prev_total")
        # ——复用判据必须能**证伪它唯一要防的那种退化**——
        # 「reused>0」证明不了任何事：每轮全量重渲染 + 同一 cache_id 时，服务端前缀匹配
        # 照样命中一大段公共前缀，占比同样是 100%。也就是说 chat 腿静默退成冷预填时，
        # 软占比这条判据全程绿灯。实测（冒烟 21 行）turn2+ 的精确关系是
        # reused == 上轮 prompt_tok + 上轮 gen_tok（206=46+160、384=224+160…），
        # 差一枚只可能来自停止符喂入与否，故按 -2 留容差做硬断言。
        # 组装侧（是否真走了增量路）与后端侧（是否真做了 token 级喂入）都要落盘，
        # 否则事后连「有没有走增量路」都无法复原。
        reuse_ok, reuse_why = True, None
        if turn >= 2 and not rotated and prev_total:
            if not used_delta:
                reuse_ok = False
                reuse_why = ("上一轮没拿到原始字节、流未提交（见上一行 committed=false）"
                             if sess.get("uncommitted")
                             else "组装侧未走增量路（stream_valid/delta 拒了）")
            elif not extra.get("delta_feed"):
                reuse_ok, reuse_why = False, "后端未走 token 级喂入（extra 无 delta_feed）"
            elif "reused_prefix" not in extra:
                reuse_ok, reuse_why = False, "extra 缺 reused_prefix：命中仪表缺失"
            elif extra.get("reuse_blocked"):
                reuse_ok, reuse_why = False, f"复用被门拒：{extra['reuse_blocked']}"
            elif (reused or 0) < prev_total - 2:
                reuse_ok = False
                reuse_why = f"命中不足：reused={reused} 期望≥{prev_total - 2}"
            elif prompt_tok and (reused or 0) > prompt_tok - 1:
                # 只设下界抓不到「reused 报成整个 prompt_tok」那种虚报（等于本轮增量
                # 一个 token 都没喂）。上界与下界一起才夹得住。
                reuse_ok = False
                reuse_why = f"命中虚报：reused={reused} ≥ prompt_tok={prompt_tok}"
        # gen_tok<=2 视为失败：§S.14 那类「同 cache 复用后首 token 即 EOT」的回归就长这样，
        # 只判 bool(text) 会让它伪装成一次「tps 偏低」的正常轮次
        alive = bool(text) and gen_tok > 2
        ok = alive and reuse_ok
        sess["prev_total"] = ((prompt_tok or 0) + gen_tok) if alive else None
        return dict(kind="chat", ok=ok, turn=turn, ttft=ttft, wall=round(wall, 3),
                    gen_tok=gen_tok, tps=(last or {}).get("generation_tps"),
                    prompt_tok=prompt_tok,
                    peak_mem=(last or {}).get("peak_memory"),
                    reused=reused, cache_rotated=rotated,
                    assembly=("delta" if used_delta else "full"),
                    delta_feed=extra.get("delta_feed"),
                    reuse_expect=(prev_total if turn >= 2 else None),
                    reuse_blocked=extra.get("reuse_blocked"),
                    reuse_diag=extra.get("reuse_diag"),
                    committed=bool(raw_text),
                    error=(None if ok else
                           (f"生成疑似秒停 gen_tok={gen_tok}" if not alive else reuse_why))
                    ), (cid or cout.get("cache_id"))

    def _tool_turn(self):
        """工具轮：prompt 必须**带工具声明段**（方言渲染），否则模型根本不知道有工具，
        guard 建起来也永不激活——初版正是这样让 21% 的负载退化成普通短聊。"""
        prompt = build_user_prompt(
            self.dialect,
            f"请立即调用 lookup_note 工具，查询关键词「{self.rng.choice(TOPICS)}」的笔记。",
            tools=[TOOL_SPEC])
        done, cout = {}, self._cout()
        last, text, ttft, cid, wall = self.client.generate(
            prompt, sampling={"max_tokens": 120, "temperature": 0.3},
            features={"tool_specs": [TOOL_SPEC]}, done_out=done, cache_out=cout)
        cid = cid or cout.get("cache_id")
        steps = done.get("constrained_steps")
        called = TOOL_SPEC["name"] in (text or "")
        # 约束一步没跑 = 这条腿没在测它该测的东西，记为失败而不是静默放过
        ok = bool(text) and steps is not None and steps > 0
        return dict(kind="tool", ok=ok, ttft=ttft, wall=round(wall, 3),
                    tps=(last or {}).get("generation_tps"),
                    peak_mem=(last or {}).get("peak_memory"),
                    constrained_steps=steps, called=called,
                    error=None if ok else f"约束未生效 constrained_steps={steps}"), cid

    def _longchain_turn(self):
        """长链：长预填 + 768 生成，打预填延迟与大 KV 的分配/回收。"""
        filler = "。".join(f"第{i}条是关于{self.rng.choice(TOPICS)}的记录，"
                          f"要点是准备{self.rng.choice(TOPICS)}相关的物品与时间安排"
                          for i in range(220))
        prompt = build_user_prompt(self.dialect, f"{filler}。请据此总结要点。")
        cout = self._cout()
        last, text, ttft, cid, wall = self.client.generate(
            prompt, sampling={"max_tokens": 768, "temperature": 0.6}, cache_out=cout)
        cid = cid or cout.get("cache_id")
        gen_tok = (last or {}).get("generation_tokens") or 0
        prompt_tok = (last or {}).get("prompt_tokens") or 0
        # 与 chat 腿同口径：只判 bool(text) 会让「首 token 即 EOT」（§S.14 那类回归）
        # 伪装成一次 tps 正常的通过；预填规模也要判，否则填充配平坏掉、这条腿退化成
        # 一次普通短生成时同样全绿。
        ok = bool(text) and gen_tok > 2 and prompt_tok > 1000
        return dict(kind="longchain", ok=ok, ttft=ttft, wall=round(wall, 3),
                    prompt_tok=prompt_tok, gen_tok=gen_tok,
                    peak_mem=(last or {}).get("peak_memory"),
                    tps=(last or {}).get("generation_tps"),
                    error=(None if ok else
                           f"长链退化 gen_tok={gen_tok} prompt_tok={prompt_tok}")), cid

    def _stream_partial(self, prompt: str, n_chunks: int):
        """开一条 /v1/generate 流，读到第 n_chunks 个 chunk 就**停在半路**，把
        (连接, 响应, info) 交出去。info 里的 `finish` 是终止原因，**不是一个布尔**：
          None="还在流中"（取消腿要的正是这个）/ "done"=自然收尾 /
          "error"=服务端错误事件 / "eof"=done 之前断流（腰斩）。
        初版把 done 与 error 一起折成 `finished=True`，调用方于是把**服务端报错**
        记成「生成先自然结束」的通过——「无错误」这条头等判据在取消腿上是反的。
        EOF 同理：`Client.generate` 明确把「done 之前 EOF」当腰斩抛错，这里不能当收尾。

        为什么不复用 Client.generate：它会把整条流读完。取消腿要的恰恰是「不读完」——
        用它写出来的取消测试会静默退化成一次普通长生成。
        """
        body = {"prompt": prompt, "sampling": {"max_tokens": 900, "temperature": 0.7},
                "features": {"want_delta": True}}
        c = self.client._conn(120.0)
        try:
            c.request("POST", "/v1/generate", body=json.dumps(body),
                      headers=self.client.headers)
            r = c.getresponse()
            if r.status >= 300:
                raise RuntimeError(f"/v1/generate -> {r.status}")
            info = {"gen_id": None, "cache_id": None, "seen": 0,
                    "finish": None, "error_payload": None}
            event = ""
            while info["seen"] < n_chunks:
                line = r.readline()
                if not line:
                    info["finish"] = "eof"
                    break
                if line.startswith(b"event:"):
                    event = line.split(b":", 1)[1].strip().decode()
                elif line.startswith(b"data:"):
                    payload = json.loads(line.split(b":", 1)[1].strip())
                    kind, event = event, ""
                    if kind == "meta":
                        info["gen_id"] = payload.get("gen_id") or info["gen_id"]
                        info["cache_id"] = payload.get("cache_id") or info["cache_id"]
                        if info["cache_id"]:      # 异常路径靠它退休，别让 KV 泄到 TTL
                            self._inflight["cache_id"] = info["cache_id"]
                    elif kind == "chunk":
                        info["seen"] += 1
                    elif kind == "error":
                        info["finish"], info["error_payload"] = "error", payload
                        break
                    elif kind == "done":
                        info["finish"] = "done"
                        break
            return c, r, info
        except Exception:
            c.close()          # 异常路径也必须关：否则服务端那次生成会跑满 900 token
            raise              # 并一直占着 gen_sem，而主循环已经走下一个请求了

    def _drain_until_end(self, c, r, budget: float = 20.0):
        """取消端点打完之后**继续读**，看流是否自己终止；返回 (是否终止, how, 秒)。

        为什么非读不可：服务端 `_sse` 的 finally 无论如何都会置 `cancel`，所以取消后
        立刻 `conn.close()` 的话，endpoint 腿走的其实是**断连**那条清理路径——两条腿
        在服务端合并成同一条，「取消双路都验过」是假的。取消的服务端语义是
        「不发终态事件、直接断流」（server.py `_sse`：cancelled 分支不 yield 终态），
        故这里期待的正常终止形态是 EOF。
        """
        t0 = time.time()
        try:
            c.sock.settimeout(budget)
        except Exception:
            pass
        while time.time() - t0 < budget:
            try:
                line = r.readline()
            except Exception as e:
                return False, f"读流异常 {type(e).__name__}", round(time.time() - t0, 2)
            if not line:
                return True, "eof", round(time.time() - t0, 2)
            if line.startswith(b"event:"):
                ev = line.split(b":", 1)[1].strip().decode()
                # **只有 EOF 才是取消的正常终止形态。**
                #  · error：服务端在这次生成里报错了（MLX 超售换页崩塌正是这个形态），
                #    把它当「流终止了，取消成功」等于把 C5 那条缺陷换个地方复活；
                #  · done：生成自己跑完了，说明这次取消**根本没起作用**。
                # 两者都终止了流，但都不算取消成功——所以 ended 与 ok 必须分开返回。
                if ev in ("done", "error"):
                    return False, ev, round(time.time() - t0, 2)
        return False, "still_streaming", round(time.time() - t0, 2)

    def _settle(self, baseline: int, timeout: float = 30.0):
        """等 active_generations 回落到基线。取消是否**真的清理干净**只有这里能证明——
        端点自己的返回值只说明「收到了取消」，不说明信号量释放了。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                m = self.client.json_call("GET", "/metrics", timeout=10)
                ag = m.get("active_generations")
                # 必须是**取到了这个键**才算数：`or 0` 会把「键没了」当成 0，于是首轮
                # 轮询即 `0 <= baseline` 成立、settled=True。断连腿**只有这一条判据**
                # （endpoint 腿还有 drain + cancelled_total 兜底），一旦键缺失，
                # 「断连全部落停」就是零证据的绿灯——正是本文件别处点名批过的 or-零。
                if isinstance(ag, int) and ag <= baseline:
                    return round(time.time() - t0, 2), True
            except Exception:
                pass
            # 不用 time.sleep：Ctrl-C 后最多多等一整个 timeout。但 stop 置位后
            # wait 会立刻返回，若不 break 就成了对 /metrics 的紧循环——每次 json_call
            # 都新开一条 TCP，正好在服务收摊那一刻给它上千条连接。
            if self.stop.wait(1.0):
                # 因信号退出 ≠ 没落停。不分开的话，SIGINT 恰好落在取消腿的轮询里就会
                # 伪造一条「active_generations 未回落」的失败，混进收官报表的错误数。
                return round(time.time() - t0, 2), None
        return round(time.time() - t0, 2), False

    def _cancel_turn(self):
        """流式中途取消：取消端点与纯断连五五开——两条清理路径都要磨，且都要证明落停。"""
        via = "endpoint" if self.rng.random() < 0.5 else "disconnect"
        prompt = build_user_prompt(self.dialect,
                                   f"详细讲讲{self.rng.choice(TOPICS)}的完整流程。")
        self._cout()
        base, base_cancels = 0, None
        try:
            m0 = self.client.json_call("GET", "/metrics", timeout=10)
            base = m0.get("active_generations") or 0
            base_cancels = m0.get("cancelled_total")
        except Exception:
            pass
        conn, resp, info = self._stream_partial(prompt, n_chunks=8)
        seen, cid = info["seen"], info["cache_id"]
        drained, cancelled = None, None
        try:
            if info["finish"] == "error":
                return dict(kind="cancel", via=via, ok=False, chunks_before=seen,
                            finish="error",
                            error=f"服务端 error 事件：{str(info['error_payload'])[:160]}"), cid
            if info["finish"] == "eof":
                # done 之前断流 = 腰斩。Client.generate 对同一形态是直接抛错的，
                # 这里当然也不能记成「自然结束」的通过。
                return dict(kind="cancel", via=via, ok=False, chunks_before=seen,
                            finish="eof",
                            error=f"done 事件前断流（腰斩），已收 {seen} chunk"), cid
            if info["finish"] == "done":
                # 8 个 chunk 之前就自然收尾：这次没测到取消，如实标注不算错
                return dict(kind="cancel", via=via, ok=True, chunks_before=seen,
                            finish="done", skipped="生成先自然结束"), cid
            if via == "endpoint":
                if not info["gen_id"]:
                    return dict(kind="cancel", via=via, ok=False, chunks_before=seen,
                                error="meta 事件没给 gen_id，取消端点无从寻址"), cid
                r = self.client.json_call(
                    "POST", f"/v1/generate/{info['gen_id']}/cancel", timeout=15)
                cancelled = bool(r.get("cancelled"))
                # 关键：**取消后不许立刻断连**，先证明流自己停了（见 _drain_until_end）
                ended, how, waited = self._drain_until_end(conn, resp)
                drained = {"ended": ended, "how": how, "waited_s": waited}
        finally:
            conn.close()      # endpoint 腿到这里流已自行终止，close 只是收句柄
        settle_s, settled = self._settle(base)   # settled 三态：True/False/None(信号中止)
        after_cancels = None
        if via == "endpoint":
            try:
                after_cancels = self.client.json_call(
                    "GET", "/metrics", timeout=10).get("cancelled_total")
            except Exception:
                pass
        # 判据分两条腿写清楚：
        #  endpoint —— 端点确认取消 + 流自行终止（非我方断连）+ cancelled_total 进位 + 落停；
        #              `cancelled=False` 是幂等返回，意思是取消到达时生成已经结束，属竞态，
        #              不算失败，但要单独记数：若大多数行都是竞态，这条腿其实没测到取消。
        #  disconnect —— 服务端对断连不发终态事件、也不计 cancelled_total，只能判落停。
        race = (via == "endpoint" and cancelled is False)
        aborted = settled is None
        whys = []
        if settled is False:
            whys.append(f"active_generations {settle_s}s 未回落")
        # 只有「未落停」这条需要三态保护——drain 与 cancelled_total 都是在 _settle
        # **之前**取到的，信号中止不该把已经拿到手的失败证据一起丢掉。
        if via == "endpoint" and not race:
            # 失败原因收集成**列表**：漏读与未落停同时发生时，只留一条会让另一条的证据
            # 在失败明细里彻底消失。
            if not (drained or {}).get("ended"):
                whys.append(f"取消后流未以 EOF 自行终止：{drained}")
            if base_cancels is not None and after_cancels is not None \
                    and after_cancels <= base_cancels:
                whys.append(f"cancelled_total 未进位 {base_cancels}→{after_cancels}")
        return dict(kind="cancel", via=via, ok=(not whys), chunks_before=seen,
                    cancelled=cancelled, race=race, drained=drained,
                    settle_aborted=aborted,
                    cancelled_total=[base_cancels, after_cancels],
                    settle_s=settle_s, error=("；".join(whys) or None)), cid

    # ---------- 缓存生命周期（删除 70% / TTL 30% 双路磨）----------
    def _retire_cache(self, cache_id, force_delete: bool = False,
                      count: bool = True) -> None:
        """count=False：起跑自检的强删不计进「缓存删除腿」——文件头承诺过自检产物
        不混进主循环的统计口径，计数器也在那条承诺里面。"""
        if not cache_id or cache_id in self.retired:
            return
        self.retired.add(cache_id)
        if not force_delete and self.rng.random() >= 0.7:
            if count:      # 不计数的退休也不该撑大泄漏容差（今天 count=False 恒配
                self.ttl_marks.append(time.time())   # force_delete=True，但别留这个坑）
            return
        if count:
            self.del_attempt += 1
        try:
            r = self.client.json_call("DELETE", f"/v1/cache/{cache_id}", timeout=15)
            if r.get("released") and count:
                self.del_ok += 1
        except Exception:
            pass                        # 计数差值会在 summary 里暴露删除腿整体失效

    def _write_profile(self) -> None:
        """起跑就落一份 profile.json。长跑若被 SIGKILL，summary.json 根本不会写，
        留下的两个 jsonl 就再也无法自证是哪个模型、哪档负载、是不是冒烟——一份 20 分钟
        的高压冒烟产物于是完全可能被当成长稳门禁结论引用。"""
        (self.out / "profile.json").write_text(json.dumps({
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "model_repo": REPO, "smoke": bool(getattr(self.args, "smoke", False)),
            "hours_target": self.args.hours, "seed": self.args.seed,
            "port": self.args.port, "mix": MIX,
            "idle_short_s": list(IDLE_SHORT), "idle_long_s": list(IDLE_LONG),
            "long_idle_every": LONG_IDLE_EVERY,
            "sample_interval_s": SAMPLE_INTERVAL, "heartbeat_s": HEARTBEAT_INTERVAL,
            "chat_turns": CHAT_TURNS, "cache_ttl_s": self.cache_ttl_s,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ---------- 起跑自检 ----------
    def _preflight(self) -> bool:
        """把整场时长押上去之前，先证明「该测的都测得到」。

        这是本工装第一原则成本最低的实现。没有它，一个配置层面的失效——比如服务端
        `constrained_tool_calls` 关着、或 llguidance 不可用（server 会永久禁用并只在
        **服务端日志**里打印）——会让工具腿 21% 的负载全程 `ok=False`，而其它腿不断把
        `consec_fail` 清零、熔断永不触发，于是烧满整场才在 summary 里看见。
        逐项：① 鉴权可用（打需鉴权的 /metrics；/health 免鉴权，钥匙错时照样绿）；
        ② 服务端跑的确实是本工装声明的模型（否则整场数据会被记到错的模型头上）；
        ③ 系统采样三个量都取得到（取不到就是「无泄漏」这条结论根本没有数据）；
        ④ 四条腿各跑一次，各自的关键仪表必须真出数。
        """
        try:
            self.base_metrics = self.client.json_call("GET", "/metrics", timeout=15)
        except Exception as e:
            print(f"❌ /metrics 不通（鉴权或服务未就绪），拒绝起跑: {str(e)[:160]}")
            return False

        # ② 模型身份
        try:
            served = self.client.json_call("GET", "/v1/models", timeout=15)
            ids = [d.get("id") for d in (served.get("data") or [])] or [served.get("id")]
        except Exception as e:
            print(f"❌ /v1/models 不通，无法确认服务端模型，拒绝起跑: {str(e)[:160]}")
            return False
        if not any(i and (i == REPO or str(i).endswith(REPO.split("/")[-1])) for i in ids):
            print(f"❌ 服务端模型 {ids} 与本工装声明的 {REPO} 不符，拒绝起跑"
                  f"（整场数据会被记到错的模型头上）")
            return False
        self.preflight["served_model"] = ids

        # ③ 系统采样三量（只验，不落 t0 行：t0 要在自检**之后**才起算，见 run()）
        base_sample = self._sample_once()
        missing = [k for k in ("server_pid", "server_rss_mb", "wired_mb")
                   if base_sample.get(k) in (None, "")]
        if missing:
            print(f"❌ 系统采样量取不到 {missing}（lsof/ps/vm_stat 失效），拒绝起跑"
                  f"——「无泄漏」这条判据将没有任何数据支撑")
            return False
        self.preflight["自检时 server_pid"] = base_sample["server_pid"]
        self.preflight["自检时 rss_mb"] = base_sample["server_rss_mb"]

        # ④ 四腿仪表自检（产物走 preflight.jsonl，不混进 req.jsonl 的统计口径）
        checks, pf_f = {}, (self.out / "preflight.jsonl").open("a", encoding="utf-8")
        sess, born = {"ps": None, "cache_id": None}, []
        try:
            history = []
            chat2 = None
            for _ in range(2):            # 两轮：turn2 才验得到增量组装与前缀命中
                chat2, _cid = self._chat_turn(sess, history)
                self._write(pf_f, {"leg": "chat", **chat2})
                print(f"  [自检] chat turn{chat2.get('turn')} ok={chat2.get('ok')} "
                      f"assembly={chat2.get('assembly')} reused={chat2.get('reused')}",
                      flush=True)
            checks["chat"] = bool(chat2 and chat2.get("ok")
                                  and chat2.get("assembly") == "delta"
                                  and chat2.get("delta_feed")
                                  and (chat2.get("reused") or 0) > 0)
            born.append(sess["cache_id"])

            rec, cid = self._tool_turn()
            self._write(pf_f, {"leg": "tool", **rec})
            born.append(cid)
            # 也要看 called：约束采样跑过 ≠ 模型真发出了工具调用。只看 steps 的话，
            # 「约束在跑但模型从不调用」这种服务端形态会通过自检，然后烧满整场才在
            # summary 里看见——正是 _preflight 存在的理由。
            checks["tool"] = bool(rec.get("ok")
                                  and (rec.get("constrained_steps") or 0) > 0
                                  and rec.get("called"))
            print(f"  [自检] tool ok={rec.get('ok')} "
                  f"constrained_steps={rec.get('constrained_steps')} "
                  f"called={rec.get('called')}", flush=True)

            rec, cid = self._longchain_turn()
            self._write(pf_f, {"leg": "longchain", **rec})
            born.append(cid)
            checks["longchain"] = bool(rec.get("ok") and (rec.get("prompt_tok") or 0) > 1000)
            print(f"  [自检] longchain ok={rec.get('ok')} "
                  f"prompt_tok={rec.get('prompt_tok')}", flush=True)

            # 取消腿要的是**一次真取消**：`skipped`（8 chunk 前自然收尾）与 `race`
            # （取消到达时已结束）都只说明这次没测到，不能当通过——否则取消端点整个坏掉
            # 时每行都是 race=ok，自检照样放行，整场的 endpoint 臂一次都没验过。
            # 二者都是低概率事件（max_tokens 900），重试三次仍拿不到干净的一次才判失败。
            for attempt in range(3):
                rec, cid = self._cancel_turn()
                self._write(pf_f, {"leg": "cancel", "attempt": attempt, **rec})
                born.append(cid)
                clean = bool(rec.get("ok") and not rec.get("race")
                             and not rec.get("skipped"))
                print(f"  [自检] cancel#{attempt} via={rec.get('via')} ok={rec.get('ok')} "
                      f"race={rec.get('race')} drained={rec.get('drained')}", flush=True)
                if clean:
                    break
            checks["cancel"] = clean
        except Exception as e:
            print(f"❌ 起跑自检抛异常（{type(e).__name__}: {str(e)[:200]}），拒绝起跑")
            return False
        finally:
            pf_f.close()
            # 自检中途抛异常时，前面几条腿已经在服务端造出了 26B 的 KV。不退休就只能等
            # 45min TTL，而下一次尝试的起跑基线会把它们吸收进泄漏容差里（C1 的复利）。
            # 自检造的缓存一律**真删**、不走 TTL 那条腿：它们必须在重取判据基线之前
            # 从服务端消失，否则会同时被算进 base_cc 与 ttl_marks，给泄漏容差重复计数。
            for _cid in born + [sess.get("cache_id"), self._inflight.get("cache_id")]:
                self._retire_cache(_cid, force_delete=True, count=False)
            self.retired.clear()   # 自检的 id 不该占用正式跑的去重表
        self.preflight["legs"] = checks
        bad = [k for k, v in checks.items() if not v]
        if bad:
            print(f"❌ 起跑自检未过：{bad} 的关键仪表没出数，拒绝起跑"
                  f"（详见 {self.out / 'preflight.jsonl'}）")
            return False
        print(f"✅ 起跑自检全过：{checks}，服务端模型 {ids}", flush=True)
        return True

    # ---------- 主循环 ----------
    def run(self) -> int:
        print(f"== 长稳起跑：{self.args.hours}h，产物 {self.out}", flush=True)
        self._write_profile()
        if not self._preflight():
            self._close_files()
            return 1
        # t0 与判据基线都在**自检之后**起算：自检要跑四条腿（含 768 token 的长链），
        # 算进 elapsed 会让 expect_samples 多算，而那段时间采样线程根本没起。
        self.t0 = time.time()
        self.deadline = self.t0 + self.args.hours * 3600
        try:
            self.base_metrics = self.client.json_call("GET", "/metrics", timeout=15)
        except Exception as e:
            print(f"❌ 自检后重取 /metrics 基线失败，拒绝起跑: {str(e)[:160]}")
            self._close_files()
            return 1
        self._write(self.sys_f, {**self._sample_once(), "note": "t0 基线"})
        self.samples += 1
        self.sampler_thread = threading.Thread(target=self.sampler, daemon=True)
        self.sampler_thread.start()
        # 主循环任何一处非预期异常（初版就是 `_req(**rec)` 抛 TypeError 直接冲出去的），
        # 都不该把整场的收官整个丢掉：summary 不写、句柄不关、判据全无。
        # 收官只在这里发生一次，`_loop` 只负责跑并交回运行态。
        try:
            status = self._loop()
        except BaseException as e:                       # noqa: BLE001 —— 收官优先
            self.verdict_blockers.append(
                f"主循环非预期异常：{type(e).__name__}: {str(e)[:200]}")
            print(f"❌ 主循环异常中止：{type(e).__name__}: {str(e)[:300]}", flush=True)
            traceback.print_exc()
            status = 3
        return self.finish(status)

    def _loop(self) -> int:
        """跑负载，返回运行态：0=跑到时限 / 2=连续失败熔断。收官不在这里。"""
        history, sess = [], {"ps": None, "cache_id": None}
        last_beat = time.time()
        while time.time() < self.deadline and not self.stop.is_set():
            kind = self.rng.choices([k for k, _ in MIX], [w for _, w in MIX])[0]
            # 在途登记处每轮开头就清：若本轮在 `_cout()` 之前就抛（比如构造 prompt 时），
            # except 分支读到的会是**上一轮早已退休的** cache_id，于是对同一份 id 再 DELETE
            # 一次——删除腿的「发起 > 成功」差值从此不再是失效信号。
            self._inflight.clear()
            try:
                if kind == "chat":
                    # _chat_turn 与其它三腿同样返回 (rec, cache_id)；这里必须解包。
                    # cache_id 已由 _chat_turn 自己写回 sess（会话跨轮要续用同一份），
                    # 故本地丢弃，退休走下面的 sess["cache_id"]。
                    rec, _ = self._chat_turn(sess, history)
                    if len(history) >= CHAT_TURNS * 2:
                        self._retire_cache(sess["cache_id"])
                        history, sess = [], {"ps": None, "cache_id": None}
                elif kind == "tool":
                    rec, cid = self._tool_turn()
                    self._retire_cache(cid)
                elif kind == "longchain":
                    rec, cid = self._longchain_turn()
                    self._retire_cache(cid)
                else:
                    rec, cid = self._cancel_turn()
                    self._retire_cache(cid)
            except Exception as e:
                rec = dict(kind=kind, ok=False,
                           error=f"{type(e).__name__}: {str(e)[:160]}")
                # 异常路径的 cache_id 只活在在途登记处里（超时/EOF 时它随异常一起丢）
                self._retire_cache(self._inflight.get("cache_id"))
                if kind == "chat":      # 只有 chat 腿失败才弃会话；别让别的腿的一次超时
                    self._retire_cache(sess["cache_id"])  # 把多轮样本连带缓存一起丢掉
                    history, sess = [], {"ps": None, "cache_id": None}
            self._req(**rec)
            # 软失败（秒停、复用退化）同样要弃会话：坏轮的 assistant 已经进了历史、
            # 也进了流，后续 4 轮全跑在这条被污染的会话上——一次事故会被放大成连续 5 次
            # 失败并误触熔断，把「服务死了」这个信号的语义弄脏。
            if kind == "chat" and not rec.get("ok"):
                self._retire_cache(sess["cache_id"])
                history, sess = [], {"ps": None, "cache_id": None}

            if self.consec_fail >= MAX_CONSEC_FAIL:
                print(f"❌ 连续 {self.consec_fail} 次失败，熔断中止"
                      f"（不空转到 {self.args.hours}h）", flush=True)
                return 2
            last_beat = self._beat(last_beat)

            long_idle = self.n % LONG_IDLE_EVERY == 0
            lo, hi = (IDLE_LONG if long_idle else IDLE_SHORT)
            nap = self.rng.uniform(lo, hi)
            if long_idle:
                print(f"[空闲] 进入「离开」窗 {nap/60:.1f}min", flush=True)
            # 心跳必须按**墙钟**：长空闲窗一睡 15-45 分钟，只在请求边界打心跳的话
            # 日志会静默大半小时，把 3 分钟级的 task-sentinel 骗成停滞告警——文件头
            # 承诺过这一点，初版没落实。故把长空闲切成心跳间隔的片来睡。
            slept, broke = 0.0, False
            while slept < nap:
                step = min(HEARTBEAT_INTERVAL, nap - slept)
                if self.stop.wait(step):
                    broke = True
                    break
                slept += step
                last_beat = self._beat(last_beat, idle=True)
            if broke:
                break
            if long_idle:
                print("[空闲] 「离开」窗结束", flush=True)
        self._retire_cache(sess["cache_id"])   # 收官前退休在手的会话缓存（否则必报假泄漏）
        # 被信号打断的跑**不能**和跑满时限返回同一个码：否则一次中途被 kill 的门禁
        # 会带着 exit 0 和「跑到时限」进台账，读的人无从分辨。
        return 4 if self.stop.is_set() else 0

    def _beat(self, last_beat: float, idle: bool = False) -> float:
        if time.time() - last_beat < HEARTBEAT_INTERVAL:
            return last_beat
        alive = self.sampler_thread.is_alive()
        print(f"[心跳] {(time.time() - self.t0)/3600:.2f}h/{self.args.hours}h "
              f"请求 {self.n} 错误 {self.errors} 采样 {self.samples}"
              f"{' 空闲窗中' if idle else ''}"
              f"{'' if alive else ' ⚠️采样线程已死'}", flush=True)
        return time.time()

    # ---------- 收官 ----------
    def finish(self, status: int = 0) -> int:
        self.stop.set()
        # 采样线程可能正卡在 _sample_once 的子进程/HTTP 超时里（最坏几十秒）。不等它
        # 就关句柄，它醒来那一写会撞 ValueError 并被自己的 except 吞掉，最后一段采样
        # 静默丢失。join 一下再关。
        if self.sampler_thread is not None:
            self.sampler_thread.join(timeout=60)
        rows, bad_lines = self._read_jsonl(self.out / "req.jsonl")
        sysrows, bad_sys = self._read_jsonl(self.out / "sys.jsonl")
        # 回读不到行 = 什么都没测到，绝不能配着 errors:0 走成「全绿」。工装自己数过
        # self.n 条，对不上就是判据不可用。
        if len(rows) != self.n:
            self.verdict_blockers.append(
                f"req.jsonl 回读 {len(rows)} 行，与工装计数 {self.n} 不符（坏行 {bad_lines}）")
        try:
            final = self.client.json_call("GET", "/metrics", timeout=15)
        except Exception as e:
            final = {"error": str(e)[:120]}
        # 末态读不到，「无泄漏/无错误」就没有立论依据。初版在这里让 cc/ag 双双取 0、
        # leaked=False，于是「跑完且无错无泄漏」的退出码建立在一次根本没读到的末态上
        # ——恰恰是服务在末尾死掉这种最该抓的形态。
        if not isinstance(final, dict) or "error" in final:
            self.verdict_blockers.append(f"收官 /metrics 读不到：{final}")

        elapsed = time.time() - self.t0
        elapsed_h = elapsed / 3600
        m, valid_sys = measure(rows, sysrows, elapsed)
        expect_samples = int(elapsed / SAMPLE_INTERVAL) + 1
        cancels = [r for r in rows if r.get("kind") == "cancel"]
        pids = {r.get("server_pid") for r in valid_sys if r.get("server_pid")}
        # 服务中途被外部重启，会给出一条从头开始的完美 RSS 平线 + 归零的 errors_total
        # ——「无泄漏无错误」。这两条断言就是防它。
        if len(pids) > 1:
            self.verdict_blockers.append(f"服务进程中途变更：server_pid 出现 {sorted(pids)}")
        g0, g1 = self.base_metrics.get("generations_total"), (final or {}).get("generations_total")
        if isinstance(g0, int) and isinstance(g1, int) and g1 < g0:
            self.verdict_blockers.append(f"generations_total 回退 {g0}→{g1}（服务重启过）")
        summary = {
            # 产物必须自报是不是冒烟：等权负载与秒级空闲下的三段读起来和长稳一模一样，
            # 没有这个标记，一份 20 分钟的冒烟产物完全可能被当成长稳门禁结论引用。
            "smoke": bool(getattr(self.args, "smoke", False)),
            "hours": round(elapsed_h, 2),
            **m,
            "缓存删除腿": {"发起": self.del_attempt, "成功": self.del_ok,
                        "交给 TTL 累计": len(self.ttl_marks),
                        "_备注": "起跑自检的强删不计入本行"},
            "采样健康": {"有效行": len(valid_sys), "总行": len(sysrows),
                       "工装计数": self.samples,
                       "应有约": expect_samples, "线程死于": self.sampler_died,
                       "坏行": {"req": bad_lines, "sys": bad_sys}},
            "metrics_首末": {k: [self.base_metrics.get(k), (final or {}).get(k)]
                            for k in ("errors_total", "generations_total", "cancelled_total")},
            "final_metrics": final,
        }
        # 「健康探针全绿」是文件头列明的头等判据，此前只统计从不断言：/health 全程 503 而
        # /metrics 仍通时，失败次数会是满额，而判据阻断仍为空、退出码仍为 0。
        hp = (m.get("健康探针") or {}).get("失败次数") or 0
        # 不做零容忍：/health 的 10s 预算要和一次 26B 生成抢资源，单次瞬时超时在 8 小时
        # 里是可能的，一次就把整场判红分辨不出「偶发」与「全程 503」。容一次或 1%，
        # 原始次数无论如何都进 summary。
        probed = sum(1 for r in sysrows
                     if ("health_ms" in r) or ("health_error" in r))
        hp_tol = max(1, int(0.01 * probed))
        if hp > hp_tol:
            self.verdict_blockers.append(
                f"健康探针失败 {hp} 次（容差 {hp_tol}／共探测 {probed} 次）")
        # 工具腿：约束采样跑过 ≠ 模型真发出了工具调用。called 占比过低说明这条 21% 的
        # 负载没在测它该测的东西。
        tl = m.get("工具腿") or {}
        if not tl.get("样本"):
            # 零样本不是「通过」：工具腿占 21%，一次都没跑到说明这条腿的结论没有依据
            self.verdict_blockers.append("工具腿零样本——这条腿的判据没有数据支撑")
        elif tl["发出调用占比"] < 0.9:
            self.verdict_blockers.append(
                f"工具腿发出调用占比 {tl['发出调用占比']}（约束在跑但模型没真调用）")

        # ---- 泄漏判据 ----
        # 期望值只算**还没到期的那几份**：`ttl_marks` 记的是逐份交出去的时刻，而不是
        # 一个只增不减的累计数。拿累计数当阈值等于把「泄漏几十份以下」全部放行——
        # 长跑下累计能涨到上百，而末态真正在册的只有最后一个 TTL 窗内的那几份。
        # 起跑时服务端就已经在册的那些不是本次跑造出来的（上一次跑留下的、别的客户端的），
        # 不减掉就是一次**必然的假 FAIL**：冒烟实测 t0 已有 2 份，末态 36 = 34 + 2，
        # 不减基线就会把一次零泄漏的跑判成泄漏。
        now = time.time()
        # 服务端 TTL 扫描每 300s 一轮（server.py `_sweep_loop`），故一份缓存的实际寿命
        # 是 [TTL, TTL+300)。窗口不给这段宽限，末态就必然偶尔多出一两份 → 假 FAIL。
        SWEEP_GRACE = 300.0
        expect_alive = sum(1 for t in self.ttl_marks
                           if now - t < self.cache_ttl_s + SWEEP_GRACE)
        base_cc = self.base_metrics.get("cache_count")
        base_cc = base_cc if isinstance(base_cc, int) else 0
        # 起跑基线只在**第一个 TTL 窗内**计入。那些缓存从 t0 起就没人碰，服务端必然在
        # 一个 TTL 窗内把它们扫掉；把它们的额度留在整场里，等于给真泄漏留了一条
        # base_cc 宽的口子——冒烟实测 t0 有 36 份，那就是「漏 36 份 26B KV 仍判无泄漏」。
        base_credit = base_cc if elapsed < self.cache_ttl_s else 0
        cc = (final or {}).get("cache_count")
        ag = (final or {}).get("active_generations")
        if not isinstance(cc, int) or not isinstance(ag, int):
            # dict 但缺键：上面那道 blocker 只挡「不是 dict / 带 error」，缺键会一路
            # 走到 isinstance 判假 → leaked=False → 静默绿灯。
            self.verdict_blockers.append(
                f"末态 metrics 缺 cache_count/active_generations：cc={cc} ag={ag}")
        leaked = (isinstance(cc, int) and cc > expect_alive + base_credit) \
            or (isinstance(ag, int) and ag > 0)
        summary["泄漏判据"] = {"末态 cache_count": cc, "TTL 窗内未到期": expect_alive,
                            "起跑基线 cache_count": base_cc, "基线计入": base_credit,
                            "允许上限": expect_alive + base_credit,
                            "交给 TTL 累计": len(self.ttl_marks),
                            "TTL 秒": self.cache_ttl_s, "扫描宽限秒": SWEEP_GRACE,
                            "active_generations": ag, "判定": "泄漏" if leaked else "无泄漏"}
        # M11：「无错误」这条头等判据此前只被记录、从不被断言。
        e0, e1 = self.base_metrics.get("errors_total"), (final or {}).get("errors_total")
        if isinstance(e0, int) and isinstance(e1, int) and e1 > e0:
            self.verdict_blockers.append(
                f"服务端 errors_total 增量 {e1 - e0}（判据要求为零）")
        # M10：endpoint 腿若几乎全是竞态，说明取消端点其实没被验到（每行都 ok）。
        # 「未测到（信号中止）」的行不该进分母：它既不是竞态也不是成功，计进去会稀释
        # 竞态占比这条阻断。
        # 分母只留「真的走到取消这一步」的行：未测到（信号中止）、生成先自然结束、
        # 以及 error/eof 早退的行都不是竞态也不是成功，计进去只会把竞态占比稀释、
        # 让阻断更难触发（假阴），方向正好反了。
        endp = [r for r in cancels
                if r.get("via") == "endpoint" and not r.get("settle_aborted")
                and not r.get("skipped") and r.get("finish") is None]
        races = sum(1 for r in endp if r.get("race"))
        if endp and races / len(endp) > 0.3:
            self.verdict_blockers.append(
                f"取消端点竞态占比 {races}/{len(endp)}——endpoint 腿多半没真取消到")
        # M8：内存判据用的是空闲态那张表；它可以在「采样健康全绿」的同时全是 null。
        idle_n = (m.get("静默态行数") or {}).get("静默", 0)
        seg_n = m.get("mem_三段_静默态_n") or {}
        # 分段下限随规模缩放：固定 3 对 8h 跑太松（实测 31/30/33），对一小时的
        # 生产权重试跑（约 13 个采样、每段 4）又只差一个倒霉样本就误阻断。
        seg_floor = max(2, expect_samples // 15)
        if idle_n < max(3, int(0.2 * expect_samples)):
            self.verdict_blockers.append(
                f"静默态采样仅 {idle_n} 行（应有约 {expect_samples} 的两成以上）"
                f"——mem_三段_静默态 这条内存判据没有数据支撑")
        # 不与上面 elif：两条同时成立时只报一条会把分段证据从档案里抹掉
        thin = {k: v for k, v in seg_n.items() if min(v) < seg_floor}
        if thin:
            self.verdict_blockers.append(
                f"静默态分段不足（下限 {seg_floor}）：{thin}"
                f"——mem_三段_静默态 对应的格只覆盖了部分时段")
        if self.sampler_died or len(valid_sys) < expect_samples * 0.8:
            self.verdict_blockers.append(
                f"采样不完整（有效 {len(valid_sys)} / 应有约 {expect_samples}，"
                f"线程死于 {self.sampler_died}）——内存类结论不可用")
        if elapsed < self.args.hours * 3600 * 0.99:
            self.verdict_blockers.append(
                f"实跑 {elapsed/3600:.2f}h 不足目标 {self.args.hours}h——长稳结论不成立")
        summary["起跑自检"] = self.preflight
        summary["判据阻断"] = list(self.verdict_blockers)   # 拷贝，不靠别名的巧合
        summary["运行态"] = {0: "跑到时限", 2: "熔断中止", 3: "异常中止",
                            4: "信号中止"}.get(status, str(status))

        try:
            (self.out / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception as e:
            # 落盘失败（磁盘满、序列化撞到意外类型）不该让整场的产物一起消失：
            # 先把能落的落下去，再把失败本身记成判据阻断。
            self.verdict_blockers.append(f"summary.json 落盘失败：{type(e).__name__}: {e}")
            try:
                (self.out / "summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
                    encoding="utf-8")
            except Exception:
                pass
        print("== 收官 ==", json.dumps(summary, ensure_ascii=False, indent=1,
                                      default=str), flush=True)
        if leaked:
            print(f"⚠️ 收官残留：cache_count={cc}（TTL 窗内未到期 {expect_alive} 份）"
                  f" active_generations={ag}", flush=True)
        for b in self.verdict_blockers:
            print(f"❌ 判据不可用：{b}", flush=True)
        self._close_files()
        if status:                      # 熔断/异常中止：运行态优先，别被质量码盖住
            return status
        return 1 if (summary["errors"] or leaked or self.verdict_blockers) else 0

    @staticmethod
    def _read_jsonl(p: Path):
        """逐行容错：被 SIGKILL 打断留下的半行不该让整场的判决整个丢掉。"""
        rows, bad = [], 0
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    bad += 1
        except Exception:
            pass
        return rows, bad


def reanalyze(d: Path, t_end, tag: str) -> int:
    """只读重算：把某个窗口的判据用**与收官同一段代码**重新算一遍并入库。

    为什么要有它：本站曾把「干净窗口径」的三段数写进台账与 commit，而那组数出自一次
    未入库的临时分析——换个人重算，边界含不含某一行、n≥5 规则、剔不剔冷轮，任一取法
    不同就是另一组数，结论甚至会翻（longchain 在两套窗口下一个升一个降）。判据必须
    可复现，故重算走 `measure()` 同一实现，产物一并入库。
    不起服务、不加载模型、不碰 GPU；只读 jsonl。
    **退出码语义独立于长稳那套**（文件头那份是运行态信号）：0=已重算 / 1=无法重算。
    """
    if not d.is_dir():
        print(f"❌ {d} 不是一个目录")
        return 1

    def rd(name):
        out = []
        fp = d / name
        if not fp.exists():
            return out
        for line in fp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out
    rows, sysrows = rd("req.jsonl"), rd("sys.jsonl")
    ts = [r["t"] for r in rows if isinstance(r.get("t"), (int, float))]
    if not ts:
        print(f"❌ {d} 里没有带数值 t 的 req.jsonl 行，无法重算")
        return 1
    orig, prof = {}, {}
    for name, holder in (("summary.json", "orig"), ("profile.json", "prof")):
        fp = d / name
        if fp.exists():
            try:
                val = json.loads(fp.read_text(encoding="utf-8"))
                if holder == "orig":
                    orig = val
                else:
                    prof = val
            except Exception:
                pass
    sys_ts = [r["t"] for r in sysrows if isinstance(r.get("t"), (int, float))]
    last_row = max(ts + sys_ts)
    # 缺省窗口必须复现 `finish()` 用的那个窗口，而 `finish()` 用的是 elapsed（含收官
    # 开销），不是「最后一条请求的 t」。若跑正好结束在长空闲窗里，两者能差到 45 分钟，
    # 三段边界随之整体平移，重算表会与 summary.json 悄悄对不上。故优先用 summary 记的
    # hours；没有才回落到最后一行 + 余量。
    if t_end is not None:
        t_end = float(t_end)
        src = "显式 --t-end"
    elif isinstance(orig.get("窗口秒"), (int, float)):
        # `窗口秒` 是 finish() 当场记下的**精确**右边界，**无条件优先**：不要拿它和
        # last_row 比大小再决定用不用——采样线程可能在 finish() 算完窗口之后才把最后
        # 一行写进 sys.jsonl（join 最长等 60s），那时 last_row > 窗口秒，一比就悄悄
        # 退回 hours 的近似窗口，恰好是精确来源存在时最不该退的场合。
        t_end, src = float(orig["窗口秒"]), "summary.json 的 窗口秒（精确复现收官口径）"
    elif isinstance(orig.get("hours"), (int, float)) and orig["hours"] * 3600 >= last_row:
        t_end, src = orig["hours"] * 3600, "summary.json 的 hours（近似，精度 ±18s）"
    else:
        t_end, src = last_row + 1e-6, "最后一行 t（summary.json 不可用）"
    if t_end <= 0:
        print(f"❌ 窗口右边界 {t_end} 非正，无法重算")
        return 1
    rows = [r for r in rows if isinstance(r.get("t"), (int, float)) and r["t"] < t_end]
    sysrows = [r for r in sysrows if isinstance(r.get("t"), (int, float)) and r["t"] < t_end]
    if not rows:
        print(f"❌ 窗口 t<{t_end} 内没有请求行")
        return 1
    # 口径必须取自被重算的那次跑（profile.json），不能读当前模块常量——见 measure()
    spec = {}
    if prof:
        spec = {"mix": [tuple(x) for x in (prof.get("mix") or [])] or None,
                "sample_interval": prof.get("sample_interval_s"),
                "来源": "profile.json"}
        spec = {k: v for k, v in spec.items() if v is not None}
    m, _valid = measure(rows, sysrows, t_end, spec=spec or None)
    dropped = sum(1 for t in ts + sys_ts if t >= t_end)
    out = {"重算窗口": {"t_end 秒": round(t_end, 1), "来源": src,
                    "t_end/末行": round(t_end / last_row, 4) if last_row else None,
                    "全程末行 t": round(last_row, 1),
                    "窗外丢弃行数": dropped, "tag": tag},
           "说明": "本文件只是同一批原始数据在指定窗口下的重算；泄漏/判据阻断/运行态"
                   "等运行态字段仍以 summary.json 为准（它们不是窗口的函数）。",
           # 产物必须自报身份；两边都没有标记时**明说不知道**，不要留一个 null
           # 让读者当成「不是冒烟」。
           "smoke": (orig.get("smoke") if isinstance(orig.get("smoke"), bool)
                     else prof.get("smoke") if isinstance(prof.get("smoke"), bool)
                     else "未知（summary/profile 均无标记）"), **m}
    (d / f"summary-{tag}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print(f"\n→ 已写 {d / f'summary-{tag}.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=None)   # 缺省按档位解析，见下
    ap.add_argument("--outdir")   # 跑长稳时必填；--reanalyze 只读重算时不需要
    ap.add_argument("--port", type=int, default=int(os.environ.get("INFER_PORT", 14161)))
    ap.add_argument("--seed", type=int, default=20260818)
    ap.add_argument("--smoke", action="store_true",
                    help="短程冒烟：四腿等权 + 秒级空闲 + 分钟级采样；验仪表不产判决")
    ap.add_argument("--reanalyze", metavar="DIR",
                    help="只读事后重算：读 DIR 的 jsonl 重算判据，不起服务、不碰 GPU")
    ap.add_argument("--t-end", type=float,
                    help="--reanalyze 的窗口右边界（秒）；缺省=收官窗口（见 reanalyze）")
    ap.add_argument("--tag",
                    help="--reanalyze 产物名后缀，落 summary-<tag>.json；"
                         "缺省按是否给了 --t-end 取 clean/full")
    ap.add_argument("--force", action="store_true",
                    help="--reanalyze 允许覆盖已存在的 summary-<tag>.json")
    args = ap.parse_args()
    if args.reanalyze is not None:      # 空串也算给了 --reanalyze，别静默落到长跑路径
        for name, val in (("--outdir", args.outdir), ("--hours", args.hours)):
            if val is not None:
                print(f"❌ {name} 与 --reanalyze 不能同用（重算是只读的）")
                return 1
        if args.smoke:
            print("❌ --smoke 与 --reanalyze 不能同用（重算的口径取自 profile.json）")
            return 1
        if args.t_end is not None and args.t_end <= 0:   # 早于产物存在性检查，
            print(f"❌ --t-end {args.t_end} 必须为正")    # 否则报错报的是另一件事
            return 1
        # tag 缺省不能固定成 clean：那样最自然的 `--reanalyze DIR`（全程窗口）会写出一份
        # 标着 clean 的**全程**结果，并直接覆盖掉已入库的干净窗产物。
        tag = args.tag or ("clean" if args.t_end is not None else "full")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", tag):
            print(f"❌ --tag {tag!r} 只允许 [A-Za-z0-9._-]，长度 1-32")
            return 1
        target = Path(args.reanalyze) / f"summary-{tag}.json"
        if target.exists() and not args.force:
            print(f"❌ {target} 已存在——重算产物是入库档案，覆盖要显式 --force"
                  f"（或换个 --tag）")
            return 1
        return reanalyze(Path(args.reanalyze), args.t_end, tag)
    if not args.outdir:
        ap.error("--outdir 是必填的（除非用 --reanalyze 做只读重算）")
    if args.smoke:
        # 缺省按档位解析：`--hours` 若也有个固定缺省值，`--smoke --outdir X` 这个最自然
        # 的写法就会被自己的护栏拒掉——护栏该拦的是**显式**写了超长时长，不是拦缺省值。
        if args.hours is None:
            args.hours = SMOKE_MAX_HOURS
        elif args.hours > SMOKE_MAX_HOURS:
            print(f"❌ --smoke 是高压短程档，--hours {args.hours} 超过上限 "
                  f"{SMOKE_MAX_HOURS}（要长跑就别带 --smoke）")
            return 1
        _apply_smoke_profile()
        print("⚠️  --smoke：负载已改为四腿等权 + 秒级空闲，本次产物**不构成长稳判决**",
              flush=True)
    elif args.hours is None:
        args.hours = LONGRUN_HOURS
    # 钥匙只走 env / .env.local——命令行参数会在 ps 里挂整场。
    # 且**不挂到 args 上**：args 是被序列化进 profile.json 的那个对象，钥匙坐在上面，
    # 离「某次重构写成 **vars(args)」只差一步，那一步的代价是钥匙进了入库产物。
    try:
        api_key = os.environ.get("INFER_API_KEY", "") or read_env_key()
    except FileNotFoundError:
        api_key = ""             # .env.local 不存在时也要落到下面那句友好拒绝，
    if not api_key:              # 而不是甩一段 traceback
        print("❌ 取不到 INFER_API_KEY（env 与 .env.local 都没有），拒绝起跑")
        return 1

    out = Path(args.outdir)
    # 零字节 jsonl 不算产物：起跑自检拒了的那次会留下两个空文件，不该把同一目录锁死
    if out.exists() and any(f.stat().st_size > 0 for f in out.glob("*.jsonl")):
        # 续跑会把两次的行合并统计而 hours 只算本次，得出的三段是错的
        print(f"❌ {out} 已有产物，拒绝在同一目录续跑（换个 outdir，或先归档）")
        return 1

    entry = model_options(REPO) or {}
    model_dir = BACKEND_DIR / "models" / REPO.split("/")[-1]
    dialect, _ = make_dialect(model_dir, entry)   # 仅分词器，CPU-only、不占 GPU

    soak = Soak(args, dialect, api_key)
    for sig in (signal.SIGINT, signal.SIGTERM):
        # 只置停止事件：正在途中的那次请求要等它自己的 socket 超时才会回来，
        # 故 Ctrl-C 到真正退出可能有分钟级延迟（首 chunk 预算 300s）。要立刻停就 kill -9。
        signal.signal(sig, lambda *_: soak.stop.set())
    return soak.run()


if __name__ == "__main__":
    sys.exit(main())
