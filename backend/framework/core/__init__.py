# 注意：不要在此处急切导入 engine/orchestrator。
# engine 依赖 memory.session，而 session 依赖本包的 task_state，
# 包级 re-export 会构成循环导入（session → core/__init__ → engine → session）。
# 请直接从具体模块导入，如：from framework.core.engine import AgentEngine
