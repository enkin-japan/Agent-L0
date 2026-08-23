"""冻结生产工具注册表的声明 schema 到 real_tool_schemas.json（题库 tools_ref 的解析源）。

为什么要快照而不是运行时直读注册表：评测须可复现（同一批题同一份 schema），而
注册表装载依赖服务探活（RAG/SearXNG 不在线的工具会被舍弃）——运行时直读会让
题库内容随环境漂移。漂移检测归金测试：快照与当前可装载工具逐项比对。

用法：cd backend && .venv/bin/python benchmarks/model_eval/items/tool_call/snapshot_real_tools.py
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))

from framework.mcp.registry import MCPRegistry  # noqa: E402

OUT = HERE / "real_tool_schemas.json"


def main() -> None:
    reg = MCPRegistry(ROOT / "framework" / "mcp" / "tools")
    schemas = {}
    for s in reg.get_all_schemas():
        name = s.get("name") or (s.get("function") or {}).get("name")
        schemas[name] = s
    payload = {"_note": "生产工具声明快照（snapshot_real_tools.py 生成；题库 task.tools_ref 按名解析）。"
                        "依赖在线服务的工具（rag_*/web_research/skill_invoke）在离线快照时缺席。",
               "tools": dict(sorted(schemas.items()))}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"✅ {len(schemas)} tools → {OUT}")


if __name__ == "__main__":
    main()
