"""memory_write：把学到的一条事实写进长期笔记本（EXECUTION_LEDGER R4）。

**分类学与「不该存什么」清单借鉴 cc-haha `memdir/memoryTypes.ts` 的 prompt 设计**
（设计，不是代码）。那份 prompt 的核心洞见：**记忆系统的失败模式不是存不下，是存太多**
——把能从代码/git/文档里推导的东西存成笔记，等于给自己攒一堆很快就会过期的假事实。
故工具描述里「不要存什么」的篇幅刻意压过「要存什么」。

写入是**无副作用**的（只动 backend/notes/ 下自己的文件），故不需审批。
"""
from ..base import BaseTool
from ...agents import runtime
from ...memory.notes import NOTE_TYPES


class MemoryWriteTool(BaseTool):
    name = "memory_write"
    description = (
        "Save ONE durable fact to your long-term notes, so you still know it in future "
        "conversations. One fact per note; give it a kebab-case name and rewrite the same "
        "name to update it.\n"
        "\n"
        "type must be one of:\n"
        "- user: who the user is — role, expertise, stable preferences.\n"
        "- feedback: how they want you to work (corrections AND confirmed approaches). "
        "Always include why.\n"
        "- project: ongoing work, goals, constraints that are NOT derivable from the code "
        "or git history. Use absolute dates, never 'today'/'last week'.\n"
        "- reference: pointers to external resources — URLs, dashboards, tickets.\n"
        "\n"
        "DO NOT save: anything already in the repo (code structure, past fixes, git history, "
        "README/CHARTER); anything that only matters to this conversation; anything you can "
        "re-derive by reading a file. A note that repeats the repo is worse than no note — "
        "it goes stale and then lies to you. When unsure, don't write.\n"
        "\n"
        "Write only when a fact is durable and non-obvious. If the user asks you to remember "
        "something that IS derivable, ask what was non-obvious about it and save that instead."
    )
    requires_approval = False   # 只写 backend/notes/ 下自己的笔记文件，无外部副作用
    is_concurrency_safe = False  # 写文件 + 重建索引，串行更省心（写入本就低频）
    is_read_only = False

    parameters = {
        "properties": {
            "name": {"type": "string",
                     "description": "kebab-case identifier, e.g. 'prefers-terse-replies'. "
                                    "Reuse an existing name to update that note."},
            "description": {"type": "string",
                            "description": "One line describing what this note holds. This is "
                                           "what you will see in future conversations to decide "
                                           "whether to read the full note — make it specific."},
            "type": {"type": "string", "enum": list(NOTE_TYPES),
                     "description": "user | feedback | project | reference"},
            "body": {"type": "string",
                     "description": "The fact itself. For feedback/project, follow with why it "
                                    "matters and how to apply it. Convert relative dates to "
                                    "absolute ('2026-07-18', not 'today'/'yesterday') — notes "
                                    "are read months later, when relative words have rotted."},
        },
        "required": ["name", "description", "type", "body"],
    }

    def is_available(self) -> bool:
        if runtime.get_note_store() is None:
            print("[MCP-memory_write] ⚠️ 笔记本未装配，工具不注册。")
            return False
        return True

    def execute(self, name: str, description: str, type: str, body: str) -> str:
        store = runtime.get_note_store()
        if store is None:
            return "System Error: note store unavailable."
        try:
            path = store.write(name, description, type, body)
        except ValueError as e:
            # 校验不过是模型手滑，就地说清楚让它重发（不是系统故障）
            return f"Note rejected: {e}"

        # 正文入 RAG 供语义召回；失败不影响记忆本身（文件已落盘、索引已重建）
        from ...memory.rag_sink import sediment_note
        indexed = sediment_note(name, description, type, body)
        note = "" if indexed else " (RAG offline — the note is saved and indexed, but " \
                                 "full-text search of it won't work until RAG is back)"
        return (f"Saved note '{name}' ({type}) to {path.name}. It will be listed in your "
                f"notes index from the next turn on.{note}")
