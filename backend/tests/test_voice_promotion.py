"""语音升格金测试（方案 B）。

纯音频消息客户端发空 prompt，摄入层 ASR 转写；本组测试守两条契约：
- ingestion.ingest 返回 voice_transcripts（path/transcript/block/marker_line），
  其余键行为不变（非升格时 ingested_text 逐字如现状）；
- turn_runner.accept_turn 在斜杠分发**之前**把空 prompt 的语音升格为本轮用户文本，
  并把 ingested_text 里对应音频的完整转写块降到仅 provenance 标记行（去重）。
"""
import asyncio
import io as _io

import pytest

from framework.core.orchestrator import Orchestrator
from framework.core.scheduler import ClassPolicy, TaskScheduler
from framework.core.turn_runner import TurnRunner
from framework.ingestion.router import Ingestion

from .fake_engine import FakeEngine, FakeRegistry

GEN = dict(temperature=0.2, min_p=0.05, enable_thinking=False,
           thinking_budget=2048, max_tokens=4096)


def run(coro):
    return asyncio.run(coro)


class _FakeUpload:
    """UploadFile 替身：router.ingest 只用 filename / file（copyfileobj + close）。"""

    def __init__(self, filename: str, data: bytes = b"RIFFfakeaudio"):
        self.filename = filename
        self.file = _io.BytesIO(data)


def _ingestion_with_asr(monkeypatch, transcripts):
    """构造一个 Ingestion（非 native_audio），把 asr.transcribe_audio 打桩成按序回放。"""
    from framework.ingestion import router as router_mod

    seq = list(transcripts)

    def fake_transcribe(path, *, model=None, language=None, provider=None):
        return seq.pop(0)

    monkeypatch.setattr(router_mod.asr, "transcribe_audio", fake_transcribe)
    return Ingestion(inference_cfg={"modalities": ["text", "image"]})


def _dirs(tmp_path):
    return (tmp_path / "img", tmp_path / "aud", tmp_path / "att")


# ---------- ingest：形状 ----------

def test_ingest_returns_voice_transcripts_shape(tmp_path, monkeypatch):
    ing = _ingestion_with_asr(monkeypatch, ["你好世界"])
    img, aud, att = _dirs(tmp_path)
    res = run(ing.ingest([_FakeUpload("memo.wav")], img, aud, att))

    vts = res["voice_transcripts"]
    assert len(vts) == 1
    vt = vts[0]
    assert set(vt) == {"path", "transcript", "block", "marker_line"}
    assert vt["transcript"] == "你好世界"
    # 落盘绝对路径，且就是 attachment 登记的那条
    assert vt["path"] == res["attachments"][0]["path"]
    # block 含转写正文 + provenance；marker_line 只是 provenance 一行
    assert "你好世界" in vt["block"] and vt["marker_line"] in vt["block"]
    assert "你好世界" not in vt["marker_line"]
    assert vt["path"] in vt["marker_line"]
    # 非升格出口：ingested_text 就是完整块（与现状逐字一致）
    assert res["ingested_text"] == vt["block"]


def test_ingest_non_audio_has_empty_voice_transcripts(tmp_path, monkeypatch):
    ing = _ingestion_with_asr(monkeypatch, [])
    img, aud, att = _dirs(tmp_path)
    res = run(ing.ingest([_FakeUpload("note.txt", b"plain text body")], img, aud, att))
    assert res["voice_transcripts"] == []
    assert "plain text body" in res["ingested_text"]


# ---------- accept_turn：升格 / 去重 / 不升格 ----------

def make_runner():
    engine = FakeEngine([{"text": "ok"}])
    orch = Orchestrator(engine, FakeRegistry([]), {
        "react_max_steps": 4, "max_total_iterations": 12, "replan_max": 3,
        "observation_max_chars": 4000, "tool_timeout_seconds": 5,
        "max_parse_retries": 3, "thinking_mode": "manual"})
    return TurnRunner(orch, TaskScheduler(policy=ClassPolicy()))


def _voice(path, transcript):
    """照 router 的拼法造一条 voice_transcript（block/marker_line 自洽）。"""
    marker = (f"[Original audio: {path}; to re-listen to a time range use "
              f"file_extract(path, start=seconds, end=seconds)]")
    block = f"[Audio transcript · {path.split('/')[-1]}]\n{transcript}\n{marker}"
    return {"path": path, "transcript": transcript, "block": block, "marker_line": marker}


def _user_msg_texts(session):
    """会话历史最后一条（本轮追加的）用户消息的文本块列表。"""
    msg = session.history[-1]
    assert msg["role"] == "user"
    return [c["text"] for c in msg["content"] if c["type"] == "text"]


def test_promote_empty_prompt_with_transcript(make_session, monkeypatch):
    monkeypatch.setattr("framework.agents.runtime.get_skill_registry", lambda: None)
    runner = make_runner()
    session = make_session()
    vt = _voice("/data/aud/a.wav", "帮我订明天的会议室")

    ctx = run(runner.accept_turn(
        session, "t-session", prompt="", images=[], audios=[],
        ingested_text=vt["block"], attachments=[], gen=dict(GEN),
        voice_transcripts=[vt]))

    # prompt 升格为转写；ingested_text 只剩 provenance 标记行（转写正文去重）
    assert ctx["prompt"] == "帮我订明天的会议室"
    assert ctx["ingested_text"] == vt["marker_line"]
    assert "帮我订明天的会议室" not in ctx["ingested_text"]
    # 用户消息文本块：正文只出现在 prompt 块，不在 extra_text 块
    texts = _user_msg_texts(session)
    assert texts[0] == "帮我订明天的会议室"
    assert texts[1] == vt["marker_line"]
    assert sum("帮我订明天的会议室" in t for t in texts) == 1


def test_no_promote_when_prompt_nonempty(make_session, monkeypatch):
    monkeypatch.setattr("framework.agents.runtime.get_skill_registry", lambda: None)
    runner = make_runner()
    session = make_session()
    vt = _voice("/data/aud/a.wav", "语音里说的内容")

    ctx = run(runner.accept_turn(
        session, "t-session", prompt="用户打的字", images=[], audios=[],
        ingested_text=vt["block"], attachments=[], gen=dict(GEN),
        voice_transcripts=[vt]))

    # prompt 原样；ingested_text 保留完整转写块（逐字如现状）
    assert ctx["prompt"] == "用户打的字"
    assert ctx["ingested_text"] == vt["block"]
    assert "语音里说的内容" in ctx["ingested_text"]


def test_silence_empty_transcript_not_promoted(make_session, monkeypatch):
    monkeypatch.setattr("framework.agents.runtime.get_skill_registry", lambda: None)
    runner = make_runner()
    session = make_session()
    vt = _voice("/data/aud/silent.wav", "   ")  # 全空白转写（静音）

    ctx = run(runner.accept_turn(
        session, "t-session", prompt="", images=[], audios=[],
        ingested_text=vt["block"], attachments=[], gen=dict(GEN),
        voice_transcripts=[vt]))

    # 不升格：prompt 仍空，摄入块原样保留
    assert ctx["prompt"] == ""
    assert ctx["ingested_text"] == vt["block"]


def test_multi_audio_join_order_and_dedup(make_session, monkeypatch):
    monkeypatch.setattr("framework.agents.runtime.get_skill_registry", lambda: None)
    runner = make_runner()
    session = make_session()
    v1 = _voice("/data/aud/a.wav", "第一段")
    v2 = _voice("/data/aud/b.wav", "第二段")
    ingested = f"{v1['block']}\n\n{v2['block']}"

    ctx = run(runner.accept_turn(
        session, "t-session", prompt="  ", images=[], audios=[],
        ingested_text=ingested, attachments=[], gen=dict(GEN),
        voice_transcripts=[v1, v2]))

    # 按序拼接
    assert ctx["prompt"] == "第一段\n第二段"
    # 两个块都降到标记行，转写正文均不残留
    assert ctx["ingested_text"] == f"{v1['marker_line']}\n\n{v2['marker_line']}"
    assert "第一段" not in ctx["ingested_text"] and "第二段" not in ctx["ingested_text"]


def test_promoted_slash_triggers_skill_dispatch(make_session, monkeypatch, tmp_path):
    """转写以 /known-skill 开头：升格后走确定性斜杠分发（剧本正文注入 ingested_text）。"""
    from framework.core.skill_registry import SkillRegistry

    (tmp_path / "playbook.md").write_text(
        "---\nname: memo\ndescription: 记录备忘\n---\n把用户口述整理成待办清单。\n",
        encoding="utf-8")
    reg = SkillRegistry(tmp_path, tool_names=set())
    monkeypatch.setattr("framework.agents.runtime.get_skill_registry", lambda: reg)

    runner = make_runner()
    session = make_session()
    vt = _voice("/data/aud/a.wav", "/memo 买牛奶和鸡蛋")

    ctx = run(runner.accept_turn(
        session, "t-session", prompt="", images=[], audios=[],
        ingested_text=vt["block"], attachments=[], gen=dict(GEN),
        voice_transcripts=[vt]))

    # 升格后 prompt 是含斜杠点名的转写；分发把剧本正文注入 ingested_text
    assert ctx["prompt"] == "/memo 买牛奶和鸡蛋"
    assert "把用户口述整理成待办清单" in ctx["ingested_text"]
    # 原音频 provenance 标记行仍在（去重后的块尾随剧本注入之后）
    assert vt["marker_line"] in ctx["ingested_text"]
    # 转写正文（用户口述）已从摄入块去重，只作为 prompt 存在一份
    assert "买牛奶和鸡蛋" not in ctx["ingested_text"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_asr_failopen_markers_not_promotable(tmp_path, monkeypatch):
    """ASR fail-open 标记（静音/不可达/解码失败）不是"用户的话"：不进 voice_transcripts
    升格通道，完整块保留在摄入文本里让模型看到失败原因。"""
    for marker in ("[Empty transcript (no speech detected)]",
                   "[Audio transcription unavailable: x]",
                   "[Audio transcription failed: y]"):
        ing = _ingestion_with_asr(monkeypatch, [marker])
        img, aud, att = _dirs(tmp_path)
        res = run(ing.ingest([_FakeUpload("memo.wav")], img, aud, att))
        assert res["voice_transcripts"] == []
        assert marker in res["ingested_text"]      # 失败原因仍在摄入块里
