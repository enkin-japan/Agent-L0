"""任务路由器金测试（混合架构第一波：CHARTER §7「任务路由器」）。

守的是什么：
① **词汇钉死**——kind / 模态需求拼错必须响亮 ValueError；静默走默认路由 =
  视觉请求落到无视觉模型，症状比报错难查十倍。
② **main 粘滞不变量**——未画像模型绝不因猜测被选中；专科须实测显著优势
  （ROUTING_MARGIN）才切换。这是「路由阈值由实测校准，不由人手填猜测」的
  代码化形态。
③ **不可满足必须 raise**——RoutingUnsatisfiable 留给消费方降级，路由器不静默回落。
④ **config 画像声明合法**——profile 键词汇受控、rates ∈ [0,1]、tps 为正
  （拼错键 = 该指标静默丢失 = 路由退化且无症状）。
⑤ **执行面接线**——excel 视觉回读经路由器（源码钉板）；引擎池守卫（未声明端点
  KeyError / inprocess 拒建 / main 直通不装配）。
"""
import asyncio
from pathlib import Path

import pytest
import yaml

from framework.core.engine_pool import EnginePool
from framework.core.model_router import (PROFILE_METRICS, ROUTE_KINDS, ROUTING_MARGIN,
                                         RoutingUnsatisfiable, select_endpoint)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _cfg(main_caps=("text", "vision"), main_profile=None, endpoints=(), extra_models=()):
    """构造注入用 inference 段：main = org/main-model，追加端点按 (name, repo) 声明。"""
    registry = [{"repo": "org/main-model", "revision": "r0", "dialect": "gemma4",
                 "capabilities": list(main_caps),
                 **({"profile": main_profile} if main_profile else {})}]
    registry += list(extra_models)
    return {"model": "org/main-model", "model_revision": "r0", "mode": "remote",
            "remote": {"host": "10.0.0.1", "port": 14161},
            "model_registry": registry,
            "endpoints": [{"name": n, "repo": r, "port": 14163 + i}
                          for i, (n, r) in enumerate(endpoints)]}


def _spec(repo, caps=("text", "vision"), profile=None):
    return {"repo": repo, "revision": "r1", "dialect": "qwen35",
            "capabilities": list(caps), **({"profile": profile} if profile else {})}


# ---- ① 词汇钉死 ----

def test_route_vocabulary_pinned():
    assert set(ROUTE_KINDS) == {"vision_qa", "tool_use", "longchain", "chat", "summary"}
    assert set(v for v in ROUTE_KINDS.values() if v) <= set(PROFILE_METRICS)
    with pytest.raises(ValueError, match="未知路由 kind"):
        select_endpoint("visoin_qa", inference_cfg=_cfg())
    with pytest.raises(ValueError, match="未知模态需求"):
        select_endpoint("vision_qa", needs=("visin",), inference_cfg=_cfg())


# ---- ②③ main 粘滞 / margin / 不可满足 ----

def test_unsatisfiable_raises_not_falls_back():
    cfg = _cfg(main_caps=("text",))  # 主模型无 vision，无追加端点
    with pytest.raises(RoutingUnsatisfiable):
        select_endpoint("vision_qa", needs=("vision",), inference_cfg=cfg)


def test_specialist_needs_significant_margin():
    spec = _spec("org/spec", profile={"tool_call": 1.00, "tps": 20.0})
    # main 0.88 vs 专科 1.00：差距 > margin → 切换
    cfg = _cfg(main_profile={"tool_call": 0.88, "tps": 30.0},
               endpoints=[("helper", "org/spec")], extra_models=[spec])
    assert select_endpoint("tool_use", inference_cfg=cfg) == "helper"
    # main 0.98 vs 专科 1.00：差距 ≤ margin → main 粘滞（切换有真实成本）
    cfg2 = _cfg(main_profile={"tool_call": 0.98, "tps": 30.0},
                endpoints=[("helper", "org/spec")], extra_models=[spec])
    assert select_endpoint("tool_use", inference_cfg=cfg2) == "main"
    assert ROUTING_MARGIN == 0.05  # margin 变更是路由行为变更，须过本测试留痕


def test_unprofiled_never_preferred():
    # 专科未画像：即便 main 画像平平也不切换（不做无依据切换）
    cfg = _cfg(main_profile={"tool_call": 0.80},
               endpoints=[("helper", "org/spec")], extra_models=[_spec("org/spec")])
    assert select_endpoint("tool_use", inference_cfg=cfg) == "main"
    # main 未画像：同样不切换（没有实测对比依据）
    cfg2 = _cfg(endpoints=[("helper", "org/spec")],
                extra_models=[_spec("org/spec", profile={"tool_call": 1.00})])
    assert select_endpoint("tool_use", inference_cfg=cfg2) == "main"


def test_main_ineligible_prefers_profiled_specialist():
    """主模型无 vision（如 Ornith 当 main）时，视觉单发落到实测最优的 vision 端点。"""
    a = _spec("org/spec-a", profile={"vision": 0.90, "tps": 10.0})
    b = _spec("org/spec-b", profile={"vision": 1.00, "tps": 20.0})
    c = _spec("org/spec-c")  # 未画像
    cfg = _cfg(main_caps=("text",),
               endpoints=[("va", "org/spec-a"), ("vb", "org/spec-b"), ("vc", "org/spec-c")],
               extra_models=[a, b, c])
    assert select_endpoint("vision_qa", needs=("vision",), inference_cfg=cfg) == "vb"


def test_metricless_kind_sticks_to_main():
    """chat/summary 类无排序指标：能力过滤后恒 main（不为普通文本单发折腾切换）。"""
    spec = _spec("org/spec", profile={"tool_call": 1.00, "longchain": 1.00})
    cfg = _cfg(main_profile={"tool_call": 0.5},
               endpoints=[("helper", "org/spec")], extra_models=[spec])
    assert select_endpoint("chat", inference_cfg=cfg) == "main"
    assert select_endpoint("summary", inference_cfg=cfg) == "main"


# ---- ④ config 画像声明合法 ----

def test_config_profiles_legal():
    inference = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))["inference"]
    profiled = []
    for entry in inference["model_registry"]:
        prof = entry.get("profile")
        if prof is None:
            continue
        profiled.append(entry["repo"])
        unknown = set(prof) - set(PROFILE_METRICS)
        assert not unknown, f"{entry['repo']} profile 含未知键 {unknown}（词汇: {PROFILE_METRICS}）"
        for k, v in prof.items():
            assert isinstance(v, (int, float)), f"{entry['repo']} profile.{k} 非数值"
            if k == "tps":
                assert v > 0
            else:
                assert 0.0 <= v <= 1.0, f"{entry['repo']} profile.{k}={v} 越出 [0,1]"
        # 画像声明 vision 指标的模型必须真的声明了 vision 能力（口径一致性）
        if "vision" in prof:
            assert "vision" in (entry.get("capabilities") or []), \
                f"{entry['repo']} 有 vision 画像却未声明 vision 能力"
    # 三个已评测模型的画像必须在位（路由的实测原料；重跑评测后回填而非删除）
    for repo in ("mlx-community/gemma-4-e4b-it-4bit", "mlx-community/Qwen3.5-9B-4bit",
                 "mlx-community/Ornith-1.0-9B-4bit"):
        assert repo in profiled, f"{repo} 缺 profile（PROFILES.md 已有实测数据）"


# ---- ⑤ 执行面接线 ----

def test_visual_qa_routes_through_router():
    src = (REPO_ROOT / "backend/framework/agents/excel_sandbox.py").read_text(encoding="utf-8")
    for anchor in ('select_endpoint("vision_qa", needs=("vision",))',
                   "get_engine_pool()",
                   "qa_engine.generate_once"):
        assert anchor in src, f"excel 视觉回读缺路由接线: {anchor}"


def test_engine_pool_guards():
    main = object()
    cfg = _cfg(endpoints=[("helper", "org/spec")], extra_models=[_spec("org/spec")])

    async def _run():
        pool = EnginePool(main, inference_cfg=cfg)
        assert await pool.engine_for("main") is main  # main 直通，零装配
        with pytest.raises(KeyError, match="未声明的推理端点"):
            await pool.engine_for("nope")
        # inprocess：追加端点拒建（16GB 物理约束，多模型常驻留生产机）
        pool2 = EnginePool(main, inference_cfg={**cfg, "mode": "inprocess"})
        with pytest.raises(RuntimeError, match="仅 remote 模式"):
            await pool2.engine_for("helper")
        pool2.shutdown_extras()  # 无追加引擎时安全空转
        assert await pool2.engine_for("main") is main

    asyncio.run(_run())


def test_runtime_pool_handle_roundtrip():
    from framework.agents import runtime
    old = runtime.get_engine_pool()
    try:
        sentinel = object()
        runtime.set_engine_pool(sentinel)
        assert runtime.get_engine_pool() is sentinel
    finally:
        runtime.set_engine_pool(old)
