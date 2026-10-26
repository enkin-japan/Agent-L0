"""Bounded, turn-local evidence accounting; never a model-visible source of facts.

Only a failed call to a trusted retrieval tool opens a pending marker. Any later
completed retrieval (evidence or an empty result, any target) releases every
pending marker; an unrelated non-retrieval tool cannot. While a marker is pending,
only true content-delivery tools are held back (see is_delivery).
The checkpoint lives in carried_turn_stats, alongside the existing pause counters.
"""
import hashlib
import json
import re

from ..cloud.degrade import CLOUD_CONSULT_TOOL

RETRIEVAL_TOOLS = frozenset({"rag_search", "rag_get_context"})
# Tools that turn model-authored content into an artifact, a persisted fact, or
# an outward send. Everything else (moves, opens, clicks, deletions, reads) is
# not a delivery and stays untouched by this guard.
DELIVERY_TOOLS = frozenset({
    # charts / documents / files / decks (model-authored content becomes an artifact)
    "render_chart", "write_document", "write_file", "edit_file",
    "deck_init", "deck_set_slide", "deck_export_pdf", "deck_export_pptx",
    # persistence of claimed facts
    "memory_write", "rag_insert_document",
    # outward sends of model-authored content
    "notify_user", "clipboard_write", "calendar_add", "reminder_add",
    "schedule_create", "schedule_update", "type_text", "set_ui_element_value",
    "run_applescript",
    # spreadsheets built from model-written text
    "excel_research",
})
SHELL_TOOL = "run_shell_command"
MAX_TARGETS = 64
REPAIR_NOTE = (
    "[System guardrail] A knowledge-base retrieval in this turn failed and has not "
    "been completed. Do not invent values, simulate a report, or present unverified "
    "facts. You may retry the retrieval with different wording, another category, "
    "or another context pointer. An unrelated non-retrieval tool does not resolve "
    "it. Otherwise stop and report that retrieval is incomplete."
)
DELIVERY_BLOCKED = (
    "System Error: delivery blocked because a knowledge-base retrieval in this turn "
    "failed and has not been completed. This operation was NOT executed. "
    "First complete a retrieval (different wording, another category, or another "
    "context pointer), or report retrieval as incomplete; do not turn simulated "
    "values into charts, files, or messages."
)


def is_delivery(registry, call):
    """True only for calls that deliver model-authored content (R1)."""
    name = call.get("name")
    args = call.get("arguments") or {}
    if name in RETRIEVAL_TOOLS or name == CLOUD_CONSULT_TOOL:
        return False
    if name == SHELL_TOOL:
        # The shell's own read-only judgment: approval == not read-only command.
        return bool(registry.requires_approval(name, args))
    if name in DELIVERY_TOOLS:
        return True
    # Dynamic API tools (api_tool_factory) carry `_method`; a mutating method
    # sends model-authored content out. Other non-read-only tools are not
    # deliveries merely because they change local state.
    tool = (getattr(registry, "_tools", None) or {}).get(name)
    return (tool is not None and hasattr(tool, "_method")
            and not registry.is_read_only(name, args))


def target_key(name, args):
    if name not in RETRIEVAL_TOOLS:
        return None
    if name == "rag_search":
        target = [name, str(args.get("category", "")),
                  " ".join(str(args.get("query", "")).split()).casefold()]
    else:
        target = [name, str(args.get("source_path", "")), args.get("seq")]
    return hashlib.sha256(json.dumps(target, ensure_ascii=False,
                                    sort_keys=True).encode()).hexdigest()


def observation(name, args, text, render, failed):
    """Classify before observation truncation; public tool status is unchanged."""
    target = target_key(name, args)
    if target is None:
        return None
    text = str(text or "").strip()
    kind = "failed"
    if not failed:
        if name == "rag_search":
            # Citations are produced by the same trusted tool loop as the text.
            payload = (render or {}).get("payload") if isinstance(render, dict) else None
            items = payload.get("items") if isinstance(payload, dict) else None
            if (isinstance(render, dict) and render.get("hint") == "citations"
                    and isinstance(items, list) and any(
                        isinstance(i, dict) and str(i.get("snippet") or "").strip()
                        for i in items)
                    and text.startswith("[Search results for '")
                    and re.search(r"\b[1-9]\d* highly relevant results found\]", text)):
                kind = "evidence"
            elif text.startswith("No results for '"):
                kind = "empty"
        elif (text.startswith("[Context around chunk ")
              and re.search(r"--- Chunk \d+(?: \(the hit\))? ---\n\S", text)):
            kind = "evidence"
        else:
            # rag_get_context reports "not found" with the RAG server's own msg
            # (e.g. 未找到该 source_path 的任何切片…), not a fixed prefix. Any
            # non-failed, non-evidence response is a completed (empty) lookup;
            # transport/tool errors are already `failed` via the failed flag.
            kind = "empty"
    return {"target": target, "kind": kind}


class RetrievalEvidence:
    def __init__(self, checkpoint=None):
        # A malformed checkpoint field is treated as empty, never raised.
        checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
        pending = checkpoint.get("pending")
        self.pending = (dict(list(pending.items())[:MAX_TARGETS])
                        if isinstance(pending, dict) else {})
        self.overflow = checkpoint.get("overflow") is True
        nudges = checkpoint.get("nudges")
        self.nudges = (min(1, max(0, nudges))
                       if isinstance(nudges, int) and not isinstance(nudges, bool) else 0)

    @property
    def unresolved(self):
        return self.overflow or bool(self.pending)

    def observe(self, calls, responses):
        for call, response in zip(calls, responses):
            target = target_key(call.get("name"), call.get("arguments") or {})
            if target is None:
                continue
            fact = response.get("_retrieval")
            # A blocked/denied/invalid call is not a new observation and cannot
            # erase an earlier completed result or missing-evidence marker.
            if not isinstance(fact, dict) or fact.get("target") != target:
                continue
            if fact.get("kind") in {"evidence", "empty"}:
                # A completed retrieval (any target) releases every pending
                # marker: retrying with other wording/category/pointer is allowed.
                self.pending.clear()
                self.overflow = False
            elif len(self.pending) < MAX_TARGETS or target in self.pending:
                self.pending[target] = "failed"
            else:
                # Bounded and fail-closed: never evict an unresolved target merely
                # because the model emitted enough new query strings.
                self.overflow = True

    def checkpoint(self):
        return {"pending": dict(self.pending), "overflow": self.overflow,
                "nudges": self.nudges}

    def explanation(self):
        return ("知识库检索尚未完成，未取得所需资料的有效证据，因此暂时无法根据你的资料回答。"
                "请稍后重试或检查知识库与计算端连接；本轮未用模拟数据补答。")
