"""长上下文 × 结构化输出 探针题生成器（⑤′-2 边界目录，2026-08-23）。

确定性（固定种子）重建 `longctx_struct.jsonl`：在百科体填充文里散落 12 条「设备巡检记录」，
任务 A = 提取全部记录为 JSON 数组（字段名固定），任务 B = 按状态分组汇总为 Markdown 表。
判分全规则（text_answer：must_all 含全部编号/读数/字段名或分组合计），零主观。

为什么另起一批而不借题：08-14 哨兵矩阵的「长 ctx×结构化」弱信号实为 JSON 工具声明体
（DSL 迁移后 26b-4bit 的 tool@32k/64k 翻正），**答复正文里的结构化产出**在长上下文下
从未被探过——这批题探的正是那半条。

已知口径（评审 2026-08-23）：填充段落编号 `（N）` 与三位读数值域重叠（48k 档 12/12 读数也
出现在编号里），故「记录在文中」只能靠 device_id 断言，读数不作存在性证据；判分看的是
**答复**而非文档，不受影响。读数/编号/日期互不为子串由 _records 重采样保证。确定性绑定
CPython `random` 实现（版本升级若改算法，drift 测试会红——那时重钉种子并重跑探针）。

用法：cd backend && .venv/bin/python benchmarks/model_eval/items/text/gen_longctx_struct.py
"""
import json
import random
from pathlib import Path

SEED = 20260823
# 目标字符数：gemma-4 分词器对本填充文实测 ≈0.81 token/字（CPU 量过：8k 档 → 8.1k tok），
# 故 nominal token × 1.24 字；实际 prompt_tokens 以 run 后 transcripts 回填 BOUNDARY.md
SIZES = {"8k": 10_000, "16k": 20_000, "32k": 40_000, "48k": 60_000}

FILLER = [
    "山脉由板块碰撞挤压隆起，风化与侵蚀又在漫长岁月里将其缓慢削平。",
    "河流自高处向低处汇集，携带泥沙在中下游沉积出宽阔的冲积平原。",
    "季风区的降水集中在夏季，冬季则受大陆高压控制而干燥少雨。",
    "珊瑚礁由造礁珊瑚的骨骼层层堆叠而成，对水温与光照极为敏感。",
    "火山喷发时释放的火山灰可在平流层停留数月，短期压低全球气温。",
    "冰川在重力作用下缓慢流动，刨蚀出 U 形谷并在末端堆出冰碛。",
    "沙漠昼夜温差大，岩石反复热胀冷缩而崩解，形成典型的物理风化。",
    "热带雨林的养分多储存在生物体内，土壤本身反而相当贫瘠。",
    "潮汐由月球与太阳的引力共同驱动，朔望时出现大潮，上下弦时为小潮。",
    "喀斯特地貌由可溶性岩石被水长期溶蚀形成，溶洞与地下河是其标志。",
    "洋流把赤道的热量输往高纬，西欧因北大西洋暖流而冬季远比同纬度温和。",
    "黄土高原的黄土由风力从干旱区搬运沉积而来，直立性强却极易被流水切割。",
]

STATUSES = ["正常", "告警", "停机"]


def _records(rng: random.Random, n: int = 12):
    """生成记录；读数互异、且不是任何编号/日期/其它读数的子串（判分 must_all 是子串匹配）。
    不满足就整组重采样（而非 assert 炸掉）——种子固定时结果恒定。"""
    while True:
        ids = set()
        recs = []
        while len(recs) < n:
            did = f"DX-{rng.randint(1000, 9999)}"
            if did in ids:
                continue
            ids.add(did)
            reading = rng.randint(100, 999) + rng.choice([0, 0, 0, 1]) * 1000  # 少数四位数
            recs.append({"device_id": did,
                         "date": f"2026-0{rng.randint(1, 8)}-{rng.randint(10, 28)}",
                         "status": rng.choice(STATUSES),
                         "reading": reading})
        rs = [str(r["reading"]) for r in recs]
        ok = len(set(rs)) == n and all(
            a not in o["device_id"] and a not in o["date"] and (a == b or a not in b)
            for a in rs for o in recs for b in rs)
        if ok:
            return recs


def _render_record(rng: random.Random, r: dict) -> str:
    forms = [
        f"【巡检记录】设备 {r['device_id']} 于 {r['date']} 例行巡检，状态：{r['status']}，仪表读数 {r['reading']}。",
        f"巡检员在 {r['date']} 检查了编号为 {r['device_id']} 的设备，读数记为 {r['reading']}，当日状态判定为{r['status']}。",
        f"记录：{r['date']}｜{r['device_id']}｜{r['status']}｜读数 {r['reading']}",
    ]
    return rng.choice(forms)


def _document(rng: random.Random, target_chars: int, recs: list) -> str:
    paras = []
    n_para = max(20, target_chars // 195)
    slots = sorted(rng.sample(range(2, n_para - 2), len(recs)))
    rec_iter = iter(recs)
    for i in range(n_para):
        body = "".join(f"（{i * 5 + j}）{FILLER[(i + j) % len(FILLER)]}" for j in range(5))
        if i in slots:
            body += _render_record(rng, next(rec_iter))
        paras.append(body)
    return "\n\n".join(paras)


def build():
    rng = random.Random(SEED)
    items = []
    for size, chars in SIZES.items():
        sub = random.Random(rng.randint(0, 10**9))
        recs = _records(sub)
        doc = _document(sub, chars, recs)
        assert all(r["device_id"] in doc for r in recs)      # 存在性只靠编号（见模块头）
        fit = ("⑤′-2 边界目录探针：答复正文里的结构化产出 × 长上下文，08-16 横评与 08-14 哨兵均未覆盖"
               "（哨兵测的是工具声明体）。判分只认编号/读数/字段名子串，零主观。")
        # 任务 A：JSON 数组提取
        items.append({
            "id": f"tx-lcs-{size}-json", "dimension": "text", "lang": "zh",
            "source": {"origin": "self", "fit": fit},
            "task": {"user": (f"下面是一份内部文档。请把其中**全部**「设备巡检记录」提取为 JSON 数组，"
                              f"每条对象恰含四个字段：device_id、date、status、reading（reading 为整数）。"
                              f"只输出 JSON，不要解释。\n\n<document>\n{doc}\n</document>")},
            "gen": {"enable_thinking": False, "max_tokens": 2048, "temperature": 0.0},
            "expect": {"type": "text", "final": {
                "must_all": [r["device_id"] for r in recs] + [str(r["reading"]) for r in recs]
                            + ['"device_id"', '"reading"', '"status"', '"date"']}},
            "_meta": {"size": size, "target_chars": chars, "n_records": len(recs)},
        })
        # 任务 B：分组汇总表（计数 + 读数合计）
        groups = {}
        for r in recs:
            g = groups.setdefault(r["status"], [0, 0]); g[0] += 1; g[1] += r["reading"]
        items.append({
            "id": f"tx-lcs-{size}-table", "dimension": "text", "lang": "zh",
            "source": {"origin": "self", "fit": fit},
            "task": {"user": (f"下面是一份内部文档。请找出其中全部「设备巡检记录」，按状态（正常/告警/停机）"
                              f"分组，输出一张 Markdown 表格，列为：状态、记录条数、读数合计。"
                              f"只输出表格，不要解释。\n\n<document>\n{doc}\n</document>")},
            "gen": {"enable_thinking": False, "max_tokens": 512, "temperature": 0.0},
            # 判分：每组一行「状态 | 条数 | 合计」三段同行成对（regex_all）——条数与合计都钉
            # （评审 M7：只钉合计时条数错也能过，而 B3 结论恰恰建在条数上）
            "expect": {"type": "text", "final": {
                "regex_all": [rf"{st}\s*\|\s*{v[0]}\s*\|\s*{v[1]}(?!\d)" for st, v in groups.items()],
                "regex_any": [r"\|"]}},
            "_meta": {"size": size, "target_chars": chars, "n_records": len(recs),
                      "groups": {s: {"count": v[0], "sum": v[1]} for s, v in groups.items()}},
        })
        # 任务 B′：同题开思考（预算 1024，qwen38 生产口径同值）——首轮探针 26b-4bit 关思考时
        # 四档全挂且条数也错，说明是「散落记录的隐式聚合」而非上下文长度；B′ 用来判
        # 「开思考能否档内自救」（动作证据：换档/出网之前先看最便宜的修法）
        import copy
        tb = copy.deepcopy(items[-1])
        tb["id"] = f"tx-lcs-{size}-table-think"
        tb["gen"] = {"enable_thinking": True, "thinking_budget": 1024, "max_tokens": 2048,
                     "temperature": 0.0}
        tb["_meta"] = {**items[-1]["_meta"], "variant": "thinking_on"}
        items.append(tb)
        # 任务 B″：关思考 + 「先列后算」指令（⑤′-2 B3 的档内自救探针，用户 08-23 拍板先跑再修）：
        # 让模型先逐条列出记录再分组计算——把"没有草稿"这个病根用指令补上，看 26b 能不能自己过。
        # 判分与 B 相同（表格行钉条数+合计）；列表段不判分。
        tl = copy.deepcopy(items[-2])
        tl["id"] = f"tx-lcs-{size}-table-list"
        tl["task"] = {"user": (f"下面是一份内部文档。请分两步作答：第一步，**逐条列出**文档中全部「设备巡检记录」"
                               f"（每条一行：设备编号、状态、读数）；第二步，根据你列出的记录，按状态"
                               f"（正常/告警/停机）分组，输出一张 Markdown 表格，列为：状态、记录条数、读数合计。"
                               f"两步都要输出，不要解释。\n\n<document>\n{doc}\n</document>")}
        tl["gen"] = {"enable_thinking": False, "max_tokens": 1536, "temperature": 0.0}
        tl["_meta"] = {**items[-2]["_meta"], "variant": "list_first"}
        items.append(tl)
    return items


def main():
    out = Path(__file__).with_name("longctx_struct.jsonl")
    items = build()
    with out.open("w", encoding="utf-8") as f:
        f.write("# 长上下文×结构化探针（⑤′-2）。由 gen_longctx_struct.py 确定性生成（seed 20260823），勿手改。\n")
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    print(f"wrote {len(items)} items → {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
