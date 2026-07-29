"""模型方言注册面（EXECUTION_LEDGER R0）。

模型家族相关的名字（gemma-4 的频道式模板、将来的 ChatML / Harmony 系）全部收编在
dialects/ 之下；框架其余部分只经本模块的工厂函数取用。与 backends/ 同构（D7 目录约定）。
惰性导入：具体方言只在真正选用时才加载。

方言的选择走 config.yaml `inference.model_registry` 每条的 `dialect` 字段——
与 `suppress_ghost_thinking` 同一条路（模型级事实跟着 repo 走）。缺省即默认方言，
故存量条目不填也不影响行为（R0 零行为变化的一环）。
"""

DEFAULT_DIALECT = "gemma4"


def get_dialect_class(name: str = ""):
    """按名字返回 ModelDialect 实现类（目录约定：dialects/<name>/）。"""
    name = name or DEFAULT_DIALECT
    if name == "gemma4":
        from .gemma4.dialect import Gemma4Dialect
        return Gemma4Dialect
    if name == "qwen35":
        from .qwen35.dialect import Qwen35Dialect
        return Qwen35Dialect
    raise ValueError(f"未知模型方言: {name!r}（可用: gemma4 | qwen35）")
