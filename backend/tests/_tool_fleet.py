"""确定性工具面共享件(非测试模块)——单一真相在 `scripts/tool_token_audit.py`。

D1 金测试复审 S4 迫出的解耦:test_grammar_headroom 模块级 importorskip(llguidance)
会连带跳过任何 import 它的测试文件;可见文本快照与预算闸对 llguidance 并无依赖,
故取数走本模块(经 importlib 载入审计脚本,与运维面同一实现——两把尺就是两处漂移)。
"""
import importlib.util
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "tool_token_audit", BACKEND / "scripts" / "tool_token_audit.py")
audit = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("tool_token_audit", audit)
_spec.loader.exec_module(audit)

collect_schemas = audit.collect_schemas          # 密封构造+生产形 skill_invoke,详见彼处
GET_TOOL_SCHEMA_NAME = audit.GET_TOOL_SCHEMA_SCHEMA["name"]
META_NAMES = (audit.UPDATE_PLAN_SCHEMA["name"], audit.COMPLETE_STEP_SCHEMA["name"],
              audit.REMEMBER_SCHEMA["name"], GET_TOOL_SCHEMA_NAME)


# 两道真分词器门（gemma4 DSL 语法、D.26 可见文本预算线 12300）都是 gemma4 口径：
# D27（2026-10-31）本机缺省改为 Qwen3.8-27B-4bit 后不再跟 model_selection 走，钉到在册
# 的 gemma4 MLX 条目，门的口径与 D.26 定线时一致。qwen35 口径实测 14181 tok 越线，
# 要不要另立 qwen35 预算线归 W-1 由用户定（docs/plans/2026-10-31-context-management-plan.md）。
GEMMA4_TOKENIZER_REPO = "mlx-community/gemma-4-31b-it-4bit"


def real_lltokenizer():
    """真 LLTokenizer(本地模型目录只载分词器;钉在册 gemma4 条目,见 GEMMA4_TOKENIZER_REPO)。
    目录/依赖不在=**可见跳过**,绝不静默绿(门跑不起来≠门说是)。llguidance 惰性载入,
    缺装只跳过用到它的测试,不连坐整个文件(复审 S4)。"""
    llhf = pytest.importorskip("llguidance.hf",
                               reason="llguidance 缺装,真分词器闸无从做起")
    local = BACKEND / "models" / GEMMA4_TOKENIZER_REPO.split("/")[-1]
    if not local.is_dir():
        pytest.skip(f"本地模型目录不在({local}),真 tokenizer 编译无从做起")
    from transformers import AutoTokenizer
    return llhf.from_tokenizer(AutoTokenizer.from_pretrained(str(local)))
