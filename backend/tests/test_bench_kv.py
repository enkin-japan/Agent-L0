"""阶段三 KV 量化格测量架的 CPU 可测面（纯函数 + 接线锚）。

守的是什么：取针埋设与宽容匹配的确定性；--kv 白名单（静默跑成另一个口径是
最坏失败）；断点续跑只认 complete 格且键含 spec_mode（混口径不许互认）；
server 的 INFER_KV_BITS 覆盖在位、启动期校验、且**绝不就地改全局配置**。
"""
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_kv.py"
_SERVER = Path(__file__).resolve().parents[2] / "inference" / "server.py"


def _load():
    spec = importlib.util.spec_from_file_location("bench_kv_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bk = _load()


def _filler(n=40):
    return "\n".join(f"第{i}句：填充句 {i}。" for i in range(n))


# ---------- 取针 ----------

def test_plant_needles_inserts_both_in_order_and_depth_bands():
    out = bk.plant_needles(_filler(100)).splitlines()
    ia = next(i for i, l in enumerate(out) if bk.NEEDLE_A in l)
    ib = next(i for i, l in enumerate(out) if bk.NEEDLE_B in l)
    assert ia < ib                              # 甲区在前乙区在后
    assert 0 < ia <= len(out) * 0.2            # ~10% 深度带（距尾部 query 最远）
    assert len(out) * 0.4 <= ib <= len(out) * 0.7
    assert len(out) == 102                      # 原行数 + 两枚针，别的一行不动


def test_plant_needles_refuses_tiny_filler():
    with pytest.raises(RuntimeError, match="过短"):
        bk.plant_needles(_filler(5))


def test_needle_values_absent_from_filler_pool_and_task():
    """针值不许出现在填充句池**与问题文本**里——否则「召回」是假证据。"""
    from bench_baseline import FILLER_SENTENCES
    pool = "".join(FILLER_SENTENCES) + bk.NEEDLE_TASK
    assert bk.NEEDLE_A not in pool and bk.NEEDLE_B not in pool


def test_needle_hit_tolerates_benign_formatting():
    """良性排版差异（空格/全角连字符）不许被误判成 KV 损伤（单向假阴性面）。"""
    for text in ("门禁码是 QK-7311。", "门禁码是 QK - 7311。", "码为QK—7311",
                 "code: QK7311"):
        hit, ctx = bk.needle_hit(text, bk.NEEDLE_A)
        assert hit and bk.NEEDLE_A.split("-")[0] in ctx
    assert bk.needle_hit("这里没有码", bk.NEEDLE_A) == (False, None)
    assert not bk.needle_hit("QK-9999", bk.NEEDLE_A)[0]


# ---------- --kv 白名单 ----------

def test_parse_kv_arms_accepts_dense_and_half_integers():
    assert bk.parse_kv_arms(" dense , 3.5 ,2.5") == ["dense", "3.5", "2.5"]
    assert bk.parse_kv_arms("8") == ["8"]


@pytest.mark.parametrize("bad", ["0.5", "-1", "nan", "inf", "quant", ""])
def test_parse_kv_arms_rejects_out_of_domain(bad):
    with pytest.raises(SystemExit):
        bk.parse_kv_arms(bad)


# ---------- 断点续跑 ----------

def test_cell_done_only_counts_complete_and_keys_spec_mode(tmp_path):
    out = tmp_path / "kv.jsonl"
    rec = {"type": "cell", "model": "m", "kv": "3.5", "ctx": 8192,
           "spec_mode": "off"}
    out.write_text(json.dumps({**rec, "complete": False}) + "\n")
    assert not bk.cell_done(out, rec)
    with out.open("a") as f:
        f.write(json.dumps({**rec, "complete": True}) + "\n")
    assert bk.cell_done(out, rec)
    # 混口径不许互认：同格但 spec_mode 不同 = 没做过
    assert not bk.cell_done(out, {**rec, "spec_mode": "entry"})
    assert not bk.cell_done(out, {**rec, "kv": "2.5"})


# ---------- server 接线锚 ----------

def test_server_kv_env_override_anchors():
    server = _SERVER.read_text(encoding="utf-8")
    assert "INFER_KV_BITS" in server
    assert '("dense", "off", "null")' in server         # 显式回稠密的口子
    assert "INFER_KV_BITS 不是数值/dense" in server      # 非数值拒启
    assert "INFER_KV_BITS 越域" in server               # 启动期域校验（半整数≥1）
    # 副本写而非就地改全局（评审：INFERENCE_CFG 全局被改是本块要防的泄漏，
    # 文本锚直接钉死变异面：全 server 不许出现对全局配置的下标赋值）
    assert '_inf_cfg = {**_inf_cfg, "kv_bits"' in server
    assert "INFERENCE_CFG[" not in server
    # 覆盖结果必须真的进装载（_inf_cfg 是 load 实参的基座；kv_persist D 方案在装载处
    # 叠了能力强开 {**_inf_cfg, "kv_persist": True}——决策在执行端，这里只开能力）
    assert '**{**_inf_cfg, "kv_persist": True})' in server


def test_harness_source_anchors():
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "sorted(c for c in ctx_arms" in src      # 臂内 ctx 升序（峰值内存口径）
    assert "server_died" in src                     # 整臂静默零数据的反面
    assert '"repetition_penalty": 1.1' in src       # 交叉臂 rep 显式落账
    assert "spec_drafted" in src                    # 交叉臂投机自证
    assert "本地模型目录缺失" in src                 # 测量窗不触下载兜底
    assert "from framework.utils.path_resolver" not in src


# ---------- run_cell 守卫（假 Client 驱动） ----------

class _FakeCli:
    def __init__(self, gen_tokens=50, spec=None, text="甲区 QK-7311 乙区 ZP-4406"):
        self.gen_tokens = gen_tokens
        self.spec = spec or {}
        self.text = text

    def generate(self, prompt, *, sampling, cache_out, first_chunk_budget):
        cache_out["cache_id"] = "c1"
        last = {"prompt_tokens": 8200, "generation_tokens": self.gen_tokens,
                "prompt_tps": 500.0, "generation_tps": 20.0,
                "peak_memory": 17.0, "extra": dict(self.spec)}
        return last, self.text, 0.5, "c1", 3.0

    def json_call(self, *a, **k):
        return {}


def test_run_cell_invalidates_instant_stop():
    ok, bad = bk.run_cell(_FakeCli(gen_tokens=1), "p", 2,
                          {"max_tokens": 192}, ctx=8192, expect_spec=False)
    assert not ok and len(bad) == 2
    assert all("秒停" in r["invalid"] for r in bad)


def test_run_cell_invalidates_cross_arm_without_spec():
    """交叉臂投机没挂上 = 口径自证证了假：run 必须作废，不许混进中位。"""
    ok, bad = bk.run_cell(_FakeCli(), "p", 2, {"max_tokens": 192},
                          ctx=8192, expect_spec=True)
    assert not ok and all("投机未挂上" in r["invalid"] for r in bad)
    ok2, bad2 = bk.run_cell(_FakeCli(spec={"spec_drafted": 40}), "p", 2,
                            {"max_tokens": 192}, ctx=8192, expect_spec=True)
    assert len(ok2) == 2 and not bad2


def test_run_cell_records_needles_and_truncation():
    ok, _ = bk.run_cell(_FakeCli(gen_tokens=192), "p", 1,
                        {"max_tokens": 192}, ctx=8192, expect_spec=False)
    assert ok[0]["truncated"] and ok[0]["needle_a"] and ok[0]["needle_b"]
    assert ok[0]["needle_context"][0] and "QK" in ok[0]["needle_context"][0]
