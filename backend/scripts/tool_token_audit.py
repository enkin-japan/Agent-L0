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
import contextlib
import importlib
import json
import os
import pkgutil
import sys
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from framework.core.orchestrator import (COMPLETE_STEP_SCHEMA, GET_TOOL_SCHEMA_SCHEMA,
                                         REMEMBER_SCHEMA, UPDATE_PLAN_SCHEMA)
from framework.dialects.gemma4.dsl import format_function_declaration
from framework.mcp.base import BaseTool


REPO_ROOT = BACKEND.parent


def _provider_key_envs():
    from framework.utils.config_loader import load_section
    provs = (load_section("cloud") or {}).get("providers") or []
    return [p.get("key_env") for p in provs if isinstance(p, dict) and p.get("key_env")]


@contextlib.contextmanager
def hermetic_construction():
    """密封构造环境——声明文本一律取确定性形态(D1 金测试复审 C1/C2/S9):
    ① 云供应商 key env 全摘除 → cloud_consult 恒为无钥回退形(带钥构造会把用户的
      供应商清单吸进声明/快照);
    ② requests.get 打断 → rag_search 恒为 'Categories known at startup: default.'
      回退形(活栈构造会吸入用户知识库类目名,且金测试面零网络 I/O);
    还原语义:环境变量逐一放回,patch 出栈即拆。"""
    saved = {k: os.environ.pop(k) for k in _provider_key_envs() if k in os.environ}
    try:
        with mock.patch("requests.get", side_effect=ConnectionError("hermetic construction")):
            yield
    finally:
        os.environ.update(saved)


def collect_schemas():
    """真构造优先(rag_search 等在 __init__ 组装 parameters,__new__ 会把它错归零参,
    复审 DC3);失败退 __new__。动态 api_tools 与 rag_admin(模块 import 即探活)不在
    清单内——本账的盲区,恰含推动 catalog 化的增长向量(用户自装 API 工具),如实注明。

    skill_invoke 取**生产形**(D1 金测试复审 C3):repo skills/ 的剧本索引在装配期追加进
    description(app.py 经 registry.load_tool 重注册),import 扫描的裸类形比生产少
    ~130 token——恰好大于减法后的全部余量,不算它的预算闸是结构性乐观。repo 剧本在
    git 内、确定性;用户目录剧本(数据根)不算,与 api_tools 同为挂账盲区。"""
    import framework.mcp.tools as tools_pkg
    out, seen = [], set()
    with contextlib.ExitStack() as stack:
        stack.enter_context(hermetic_construction())
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
    out = _swap_production_skill_invoke(out)
    return out + [UPDATE_PLAN_SCHEMA, COMPLETE_STEP_SCHEMA, REMEMBER_SCHEMA,
                  GET_TOOL_SCHEMA_SCHEMA]


def _ast_tool_names(module: str) -> set:
    """AST 静态抽某工具模块的类级 name 常量——rag_admin import 即网络探活,不能进程内
    枚举,但其工具名必须进剧本探活面(否则 ingest 被舍弃,生产索引 5 份这里只算 4 份,
    预算闸又乐观一截)。纯语法抽取,零 import 零网络。"""
    import ast as _ast
    src = (BACKEND / "framework" / "mcp" / "tools" / f"{module}.py").read_text()
    out = set()
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.ClassDef):
            for st in node.body:
                if (isinstance(st, _ast.Assign) and len(st.targets) == 1
                        and getattr(st.targets[0], "id", "") == "name"
                        and isinstance(st.value, _ast.Constant)):
                    out.add(st.value.value)
    return out


def _swap_production_skill_invoke(schemas):
    """skill_invoke 换成带 repo 剧本索引的生产形;装不出=如实保留裸类形并警告,
    绝不静默(索引 ~130 token,漏算即预算闸乐观)。runtime 全局注册表用后还原。"""
    from framework.agents import runtime
    from framework.core.skill_registry import SkillRegistry
    names = {sc["name"] for sc in schemas} | _ast_tool_names("rag_admin")
    saved_reg = runtime.get_skill_registry()
    try:
        reg = SkillRegistry(REPO_ROOT / "skills", tool_names=names)
        if not reg.names():
            print("[audit] ⚠️ repo 剧本 0 份可装配——skill_invoke 按裸类形计,预算偏乐观",
                  file=sys.stderr)
            return schemas
        runtime.set_skill_registry(reg)
        from framework.mcp.tools.skill_invoke import SkillInvokeTool
        prod = SkillInvokeTool().get_schema()
        return [prod if sc["name"] == "skill_invoke" else sc for sc in schemas]
    finally:
        runtime.set_skill_registry(saved_reg)


def make_counter():
    """返回 (计数函数, 来源标签)。

    ⚠️ 刻意**不用** rag 计算面 /tokenize(复审 DC4):它数的是 embedding/rerank
    tokenizer 的 token,而 stable_prefix_tokens_est 锚的是**推理模型**的上下文预算——
    单位不同,拿它校准等于把承重参数重新锚到错误的尺上。模型经 model_selection
    决议(与引擎同源),只载 tokenizer 不载权重。
    """
    try:
        from framework.utils import model_selection
        model = model_selection.resolve_selected_model()[0]   # 与引擎同源(含 .model-selection)
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

    # full 口径与生产一致:get_tool_schema 只在 catalog 模式进声明
    # (orchestrator._meta_schemas,阶段复审 L6——初版把它算进 full,高估 ~96 token)
    full_schemas = [x for x in schemas if x["name"] != GET_TOOL_SCHEMA_SCHEMA["name"]]
    rows = []
    full_chars = 0
    for s in full_schemas:
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
