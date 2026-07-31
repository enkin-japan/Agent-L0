"""L0 脱敏评测题库金测试（混合架构第 3 站 b）：引用完整性与鉴定纪律。

守的是什么：must_mask/must_keep 必须逐字存在于 payload——引用错字 = 该题永远
失败/永远虚过且原因隐蔽（检索评估集同款教训）；同题遮/留清单不得互相包含
（判分自相矛盾）；fit 鉴定纪律（评测框架同款：每题必须说清为何贴合本项目）。
"""
import json
from pathlib import Path

ITEMS = Path(__file__).resolve().parents[1] / "benchmarks" / "l0_eval" / "items.jsonl"

_KINDS = {"name", "address", "org", "account", "mixed", "injection", "negative"}


def _load():
    out = []
    for line in ITEMS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out


def test_items_structure_and_verbatim():
    items = _load()
    assert len(items) >= 20
    ids = [it["id"] for it in items]
    assert len(ids) == len(set(ids)), "题 id 重复"
    for it in items:
        assert it["kind"] in _KINDS, f"{it['id']} 未知 kind {it['kind']}"
        assert len(it.get("fit", "")) >= 8, f"{it['id']} 缺鉴定说明（fit）"
        for s in it["must_mask"] + it["must_keep"]:
            assert s in it["payload"], f"{it['id']} 引用了不存在于 payload 的串: {s!r}"
        for m in it["must_mask"]:
            for k in it["must_keep"]:
                assert m not in k and k not in m, \
                    f"{it['id']} 遮/留清单互相包含: {m!r} vs {k!r}（判分自相矛盾）"
        assert it["must_mask"] or it["kind"] == "negative", \
            f"{it['id']} 非 negative 却无 must_mask"


def test_kind_coverage_and_probes():
    items = _load()
    kinds = {it["kind"] for it in items}
    assert kinds == _KINDS, f"形态缺失: {_KINDS - kinds}"
    # 注入探针与过遮探针是本题库的判别性核心，必须在位
    assert sum(1 for it in items if it["kind"] == "injection") >= 2
    negs = [it for it in items if it["kind"] == "negative"]
    assert len(negs) >= 3 and all(it["must_keep"] for it in negs)


def test_runner_judges_with_production_sanitize():
    """跑批器必须经生产同款 sanitize 全路（规则+模型双腿）——只测模型腿会漏掉
    双腿协同缺陷（mixed 形态存在的全部理由）。"""
    src = (ITEMS.parent / "run_l0_eval.py").read_text(encoding="utf-8")
    assert "asyncio.to_thread(eg.sanitize, it[\"payload\"]" in src, \
        "跑批器须与生产同形：工具线程同步调 sanitize + 模型腿投回主循环"
    assert "must_mask" in src and "must_keep" in src
