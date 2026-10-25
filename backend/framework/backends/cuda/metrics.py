"""Reduce real vLLM counters without exposing registry labels or guessing activity."""
import math

_NAMES = {"vllm:spec_decode_num_draft_tokens": "draft_tokens",
          "vllm:spec_decode_num_accepted_tokens": "accepted_tokens",
          "vllm:spec_decode_num_drafts": "num_drafts"}


def unknown_metrics(reason="unavailable"):
    return {"status": "unknown", "source": "vllm", "reason": reason,
            "draft_tokens": None, "accepted_tokens": None, "num_drafts": None}


def speculative_counters(metrics):
    values = {}
    for metric in metrics:
        key = _NAMES.get(getattr(metric, "name", ""))
        if key is None:
            continue
        value = getattr(metric, "value", None)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 or int(value) != value):
            return unknown_metrics("invalid_counter")
        values[key] = values.get(key, 0) + int(value)
    # No counter is not zero. An older backend/logger or disabled stats may expose
    # no samples even while a speculative configuration was requested.
    if any(key not in values for key in _NAMES.values()):
        return unknown_metrics("counters_missing")
    return {"status": "observed", "source": "vllm", **values}
