"""T6 OpenAI 入口门面纯逻辑层金测试（inference/openai_facade.py，按路径装载）。

server.py 导入即拉起 FastAPI/config，测试碰不得——门面的可测逻辑全部抽在
openai_facade.py（零重依赖），此处钉：消息校验（只收纯文本）、方言消息整形、
采样参数映射（0 值陷阱）、两种响应体的 OpenAI 兼容形状。
"""
import importlib.util
import json
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parents[2] / "inference" / "openai_facade.py"
_spec = importlib.util.spec_from_file_location("openai_facade", _MOD_PATH)
oai = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oai)


# ---------- validate_messages ----------

def test_validate_rejects_shapes():
    assert oai.validate_messages(None) is not None
    assert oai.validate_messages([]) is not None
    assert oai.validate_messages([{"role": "tool", "content": "x"}]) is not None
    assert oai.validate_messages([{"role": "user", "content": {"k": 1}}]) is not None


def test_validate_rejects_media_parts_points_to_native_api():
    err = oai.validate_messages([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "http://x"}}]}])
    assert err is not None and "/v1/generate" in err


def test_validate_accepts_text_and_parts():
    assert oai.validate_messages([
        {"role": "system", "content": "s"},
        {"role": "user", "content": [{"type": "text", "text": "a"},
                                     {"type": "text", "text": "b"}]},
        {"role": "assistant", "content": "c"},
    ]) is None


def test_to_dialect_messages_flattens_parts():
    out = oai.to_dialect_messages([
        {"role": "user", "content": [{"type": "text", "text": "甲"},
                                     {"type": "text", "text": "乙"}]}])
    assert out == [{"role": "user", "content": "甲\n乙"}]


# ---------- parse_sampling ----------

def test_sampling_defaults_and_priority():
    s = oai.parse_sampling({})
    assert s == {"max_tokens": 1024, "temperature": 0.7}
    s = oai.parse_sampling({"max_tokens": 50, "max_completion_tokens": 99})
    assert s["max_tokens"] == 99                       # 新字段优先


def test_sampling_temperature_zero_preserved():
    assert oai.parse_sampling({"temperature": 0})["temperature"] == 0.0   # 0 不是"未设"


# ---------- 响应体形状 ----------

def test_completion_response_shape():
    r = oai.completion_response("m", "chatcmpl-x", "你好", 10, 5)
    assert r["object"] == "chat.completion"
    assert r["choices"][0]["message"] == {"role": "assistant", "content": "你好"}
    assert r["choices"][0]["finish_reason"] == "stop"
    assert r["usage"] == {"prompt_tokens": 10, "completion_tokens": 5,
                          "total_tokens": 15}


def test_stream_lines_parse_as_openai_chunks():
    head = oai.stream_line("m", "cid", role="assistant")
    mid = oai.stream_line("m", "cid", content="片段")
    fin = oai.stream_line("m", "cid", finish_reason="stop")
    for line in (head, mid, fin):
        assert line.startswith("data: ") and line.endswith("\n\n")
    assert json.loads(mid[6:])["choices"][0]["delta"] == {"content": "片段"}
    assert json.loads(fin[6:])["choices"][0]["finish_reason"] == "stop"
    assert oai.done_line() == "data: [DONE]\n\n"


def test_finish_reason_boundary():
    assert oai.finish_reason_of(99, 100) == "stop"
    assert oai.finish_reason_of(100, 100) == "length"


def test_models_response():
    r = oai.models_response("gemma-4-e4b")
    assert r["data"][0]["id"] == "gemma-4-e4b" and r["object"] == "list"
