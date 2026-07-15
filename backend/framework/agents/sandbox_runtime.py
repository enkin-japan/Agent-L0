"""沙箱执行器共用件（EXECUTION_LEDGER R1-1）：一次性会话替身的唯一实现。

**为什么收编**：此前 `_EphemeralSession` 有 5 份近乎逐字相同的拷贝——research /
math / excel 各一份、excel 的视觉 QA 复用其一、orchestrator 的目标审计还抄了第五份
（`_AuditSession`，其 docstring 自陈「与 agents/research_sandbox._EphemeralSession
同型」）。三条隔离不变量（CHARTER §3）由此散在 5 处注释里，改一处修不了另外四处。

**契约面从哪来**：本类的字段集不是设计出来的，是 `engine.generate_once` 读什么就有
什么——`brain_memory_state` / `history` / `images` / `audios` / `cancel_requested`
/ `inject_env_context`。它刻意**不是** ChatSession 的子类：沙箱会话不落盘、无 meta、
无记忆写回，少一个字段就少一条泄漏路径。

守的三条不变量（test_sandbox_invariants.py 钉死）：
 · **KV 隔离**——`brain_memory_state` 必取 `backend.new_cache()`，不碰真实会话 KV。
 · **不落盘**——不暴露 append_log / save_memory / save_meta / history_log 任一面。
 · **隐私边界**——`inject_env_context = False` 显式写死。⚠️ 此前这条是靠「5 份拷贝
   都恰好忘了写这个属性」+ 引擎 `getattr(..., False)` 兜底而**偶然**成立的；哪份
   拷贝哪天手抖加上它，个人环境事实就会静默流进沙箱。故此处显式设死。
"""
from datetime import datetime
from typing import Dict, List, Optional


class EphemeralSession:
    """一次性会话替身：只暴露 generate_once 读取的字段，全新 KV、用完即弃。不落盘。

    time_anchor：注入分钟级时间锚。主编排轮次刻意只注日粒度（KV 缓存跨天友好，精确
    时间靠 get_current_time 工具），但沙箱没有时间工具、KV 又是一次性的——直接注到
    分钟+时区零缓存代价。缺锚时模型对天气/时刻类问题会触发「无实时能力」的先验拒答。
    """

    #: 隐私边界：个人环境事实（位置/语言/画像）绝不进沙箱子生成上下文。
    #: 显式写死而非依赖引擎的 getattr 兜底——见模块 docstring。
    inject_env_context = False

    def __init__(self, backend, system_prompt: str, opening: str,
                 images: Optional[List[str]] = None, time_anchor: bool = False):
        self.brain_memory_state = backend.new_cache()

        if time_anchor:
            _anchor = datetime.now().astimezone().strftime("%Y-%m-%d (%A) %H:%M %Z (UTC%z)")
            system_prompt = f"{system_prompt}\n\nCurrent date & time: {_anchor}"

        content: object = opening
        if images:
            content = [{"type": "image"} for _ in images]
            content.append({"type": "text", "text": opening})

        self.history: List[Dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]
        self.images: List[str] = list(images or [])
        self.audios: List[str] = []
        self.cancel_requested = False
