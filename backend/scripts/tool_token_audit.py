"""D0:稳定前缀的 per-tool token 真账（只读,零改动,可与任何工作并行）。

全项目最贵的常驻开销此前唯一的度量是 chars/4 启发式——而它不是仪表,是**承重参数**
（摘要高低阈值锚定在 stable_prefix_tokens_est 上,估算偏差直接推移压缩触发点）。
本脚本用真分词器逐工具计数,输出三样:
① full 模式真实总量 vs chars/4 估算的偏差（摘要阈值锚得准不准,一眼见底）;
② token 花费 top-10 的工具（TOOL_NORMS_AUDIT 的成本纪律第一次有依据）;
③ 同一份数据算出的 catalog 模式总量。

分词器:**推理模型**的本地 tokenizer(经 model_selection 决议,只载分词器不载权重;
刻意不用 rag /tokenize——那是 embedding 侧的尺,单位不同)→ 退 chars/4(①随即无意义,
输出如实改标「无真分词器」)。工具清单与 test_grammar_headroom 同口径:真构造优先、
排除 rag_admin(import 即探活)——动态 api_tools 与它同为本账盲区,恰含推动 catalog
化的增长向量,读数时记得这层。
"""
import importlib
import json
import pkgutil
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from framework.core.orchestrator import (COMPLETE_STEP_SCHEMA, GET_TOOL_SCHEMA_SCHEMA,
                                         REMEMBER_SCHEMA, UPDATE_PLAN_SCHEMA)
from framework.dialects.gemma4.dsl import format_function_declaration
from framework.mcp.base import BaseTool


def collect_schemas():
    """真构造优先(rag_search 等在 __init__ 组装 parameters,__new__ 会把它错归零参,
    复审 DC3);失败退 __new__。动态 api_tools 与 rag_admin(模块 import 即探活)不在
    清单内——本账的盲区,恰含推动 catalog 化的增长向量(用户自装 API 工具),如实注明。"""
    import framework.mcp.tools as tools_pkg
    out, seen = [], set()
    for info in pkgutil.iter_modules(tools_pkg.__path__):
        if info.name == "rag_admin":
            continue          # 模块体 import 即 3s 网络探活;与金测试同口径排除,挂账盲区
        try:
            mod = importlib.import_module(f"framework.mcp.tools.{info.name}")
        except Exception:
            continue
        for obj in vars(mod).values():
            if (isinstance(obj, type) and issubclass(obj, BaseTool) and obj is not BaseTool
                    and getattr(obj, "name", "") and obj.name not in seen):
                try:
                    inst = obj()
                except Exception:
                    try:
                        inst = obj.__new__(obj)
                    except Exception:
                        continue
                try:
                    out.append(inst.get_schema())
                    seen.add(obj.name)
                except Exception:
                    continue
    if len(out) < 40:
        print(f"[audit] ⚠️ 工具清单只收到 {len(out)} 件——枚举路径可疑,数字别进台账",
              file=sys.stderr)
    return out + [UPDATE_PLAN_SCHEMA, COMPLETE_STEP_SCHEMA, REMEMBER_SCHEMA,
                  GET_TOOL_SCHEMA_SCHEMA]


def make_counter():
    """返回 (计数函数, 来源标签)。

    ⚠️ 刻意**不用** rag 计算面 /tokenize(复审 DC4):它数的是 embedding/rerank
    tokenizer 的 token,而 stable_prefix_tokens_est 锚的是**推理模型**的上下文预算——
    单位不同,拿它校准等于把承重参数重新锚到错误的尺上。模型经 model_selection
    决议(与引擎同源),只载 tokenizer 不载权重。
    """
    try:
        from framework.utils import model_selection
        model = model_selection.resolve_model()
    except Exception:
        from framework.utils.config_loader import load_section
        model = (load_section("inference") or {}).get("model", "")
    local = BACKEND / "models" / str(model).split("/")[-1]
    try:
        if local.is_dir():
            from transformers import AutoTokenizer
            tok = AutoTokenizer.from_pretrained(str(local))
            return (lambda text: len(tok.encode(text))), f"local tokenizer ({local.name})"
    except Exception as e:
        print(f"[audit] 本地 tokenizer 不可用: {e}", file=sys.stderr)
    return (lambda text: len(text) // 4), "chars/4"


def main() -> int:
    schemas = collect_schemas()
    count, source = make_counter()
    print(f"[audit] 分词器: {source};工具 {len(schemas)} 件\n")

    rows = []
    full_chars = 0
    for s in schemas:
        decl = format_function_declaration(s)
        est = len(json.dumps(s, ensure_ascii=False)) // 4     # 生产估算的同款口径
        rows.append((s["name"], count(decl), est, len(decl)))
        full_chars += len(json.dumps(s, ensure_ascii=False))
    rows.sort(key=lambda r: -r[1])

    total_real = sum(r[1] for r in rows)
    total_est = sum(r[2] for r in rows)
    print("── ② token 花费 top-10 ──")
    print(f"{'tool':32s} {'real':>6s} {'est':>6s} {'decl_chars':>10s}")
    for name, real, est, chars in rows[:10]:
        print(f"{name:32s} {real:6d} {est:6d} {chars:10d}")

    print("\n── ① full 模式总量 ──")
    # 生产口径是「先求和再地板除」(orchestrator :360),逐工具地板会累积至多 ~60 token 漂移
    total_est_prod = full_chars // 4
    dev = (total_real - total_est_prod) / total_est_prod * 100 if total_est_prod else 0
    if source == "chars/4":
        print(f"⚠️ 无真分词器:两栏同尺,偏差恒 0,本节无意义")
    print(f"真实 {total_real} token / chars-4 估算 {total_est_prod} token / 偏差 {dev:+.1f}%")
    print("   （est 即生产 stable_prefix_tokens_est 的口径——摘要双阈值锚在它上面;"
          "两栏均不含每工具 <|tool>/<tool|> 包裹标记,~2 token/件,方向一致不影响偏差号）")

    metas = {UPDATE_PLAN_SCHEMA["name"], COMPLETE_STEP_SCHEMA["name"],
             REMEMBER_SCHEMA["name"], GET_TOOL_SCHEMA_SCHEMA["name"]}
    catalog_real = sum(
        # 生产的 catalog 面对元工具保留**完整** schema(orchestrator._build_tools_declaration
        # 的 _meta_schemas 不瘦身,复审 S6——初版把元工具也瘦了,少算 ~380 token)
        count(format_function_declaration(s)) if s["name"] in metas
        else count(format_function_declaration(
            {"name": s["name"],
             "description": str(s.get("description", "")).split("\n")[0]}))
        for s in schemas)
    print("\n── ③ catalog 模式总量 ──")
    print(f"目录常驻 {catalog_real} token（另加 get_tool_schema 按需取详情的交互成本）")
    print(f"full → catalog 立省 {total_real - catalog_real} token")
    return 0


if __name__ == "__main__":
    sys.exit(main())
