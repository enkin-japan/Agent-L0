"""ASR 的 CUDA 腿金测试（HETERO_COMPUTE A3：faster_whisper provider）。

守的是什么（faster-whisper/ctranslate2 不装在开发 Mac——用假模块测形状与纯逻辑，
不测转写质量，那归 A100 站的真机验证）：
① **注册面**——faster_whisper 名字可取；与另两条腿签名一致（互为替身）；错名报错
  文案列全四个候选。
② **auto 的解析顺序**——mode=remote 恒走 remote（旧语义不回退）；本机转写按平台
  分腿（darwin=mlx_whisper / 其余=faster_whisper），首选没装而对家装了就换腿，
  两条都没装仍返回平台首选（报错要指向这台机器本该装的那条腿）。
③ **fail-open 契约**——faster-whisper 未装、ctranslate2 不可用、无 CUDA 设备，
  三种都抛 AsrProviderUnavailable（调用方据此收成标记文本，绝不打断整轮对话）。
④ **模型名映射**——config 的 asr_model 现值是 mlx-community/ 的 MLX 专用件，本腿
  必须翻译成自家默认；显式的 CT2 仓库/本地目录原样透传。
⑤ **转写路径纯逻辑**——segments 生成器要消费完、language=None 即自动检测、模型
  按名单例复用、区间切片在内存里做（不落临时文件）。
"""
import sys
import types

import pytest

import framework.backends as B
from framework.backends import AsrProviderUnavailable, get_asr_provider
from framework.backends.cuda import asr as cuda_asr


# ---- 假的 faster_whisper / ctranslate2（开发 Mac 上这两个包都不存在）----

class _FakeSegment:
    def __init__(self, text):
        self.text = text


class _FakeWhisperModel:
    """记录构造参数与每次 transcribe 的入参，供断言。"""
    built = []

    def __init__(self, name, device=None, compute_type=None, download_root=None):
        self.name = name
        self.device = device
        self.compute_type = compute_type
        self.download_root = download_root
        self.calls = []
        _FakeWhisperModel.built.append(self)

    def transcribe(self, audio, language=None, **kw):
        self.calls.append({"audio": audio, "language": language, **kw})
        return iter([_FakeSegment(" 你好"), _FakeSegment("世界 ")]), {"language": "zh"}


def _install_fake_stack(monkeypatch, *, cuda_devices=1, decode=None):
    """把假的 faster_whisper / faster_whisper.audio / ctranslate2 塞进 sys.modules。"""
    fw = types.ModuleType("faster_whisper")
    fw.WhisperModel = _FakeWhisperModel
    audio_mod = types.ModuleType("faster_whisper.audio")
    audio_mod.decode_audio = decode or (lambda src, sampling_rate=16000: list(range(16000)))
    fw.audio = audio_mod
    ct2 = types.ModuleType("ctranslate2")
    ct2.get_cuda_device_count = lambda: cuda_devices
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", audio_mod)
    monkeypatch.setitem(sys.modules, "ctranslate2", ct2)
    return fw


@pytest.fixture(autouse=True)
def _clear_model_singleton():
    """模型单例是模块级缓存——测试间必须清空，否则前一个测试的假模型会漏进下一个。"""
    cuda_asr._MODELS.clear()
    _FakeWhisperModel.built.clear()
    yield
    cuda_asr._MODELS.clear()


# ---- ① 注册面 ----

def test_registry_dispatches_faster_whisper():
    assert get_asr_provider("faster_whisper") is cuda_asr.transcribe


def test_three_asr_providers_share_one_signature():
    """三条腿互为替身：签名不一致就会炸在调用点（ingestion/parsers/asr.py）。"""
    import inspect
    sigs = {p: set(inspect.signature(get_asr_provider(p)).parameters)
            for p in ("mlx_whisper", "faster_whisper", "remote")}
    assert sigs["faster_whisper"] == {"src", "model", "language", "start", "end"}
    assert len(set(map(frozenset, sigs.values()))) == 1, f"签名不一致: {sigs}"


def test_unknown_provider_lists_all_candidates():
    with pytest.raises(ValueError, match="faster_whisper"):
        get_asr_provider("whisper_cpp")


# ---- ② auto 的解析顺序 ----

def _stub_env(monkeypatch, *, mode="inprocess", installed=()):
    import framework.utils.config_loader as CL
    monkeypatch.setattr(CL, "load_section", lambda name: {"mode": mode})
    monkeypatch.setattr(B, "_installed", lambda pkg: pkg in installed)


@pytest.mark.parametrize("platform,both_installed", [
    ("darwin", "mlx_whisper"), ("linux", "faster_whisper"), ("win32", "faster_whisper")])
def test_auto_prefers_the_leg_of_this_platform(monkeypatch, platform, both_installed):
    monkeypatch.setattr(sys, "platform", platform)
    _stub_env(monkeypatch, installed=("mlx_whisper", "faster_whisper"))
    assert B._resolve_auto() == both_installed
    assert B.resolve_local_asr_provider() == both_installed


@pytest.mark.parametrize("platform,present", [
    ("darwin", "faster_whisper"), ("linux", "mlx_whisper")])
def test_auto_falls_back_to_the_other_leg_when_preferred_missing(
        monkeypatch, platform, present):
    """auto 的承诺是"在任一可用腿上都能工作"：首选没装而对家装了就换腿。"""
    monkeypatch.setattr(sys, "platform", platform)
    _stub_env(monkeypatch, installed=(present,))
    assert B._resolve_auto() == present


@pytest.mark.parametrize("platform,expected", [
    ("darwin", "mlx_whisper"), ("linux", "faster_whisper")])
def test_auto_with_nothing_installed_still_names_the_platform_leg(
        monkeypatch, platform, expected):
    """两条都没装时报错要指向这台机器**本该**装的那条腿，而不是对家。"""
    monkeypatch.setattr(sys, "platform", platform)
    _stub_env(monkeypatch, installed=())
    assert B._resolve_auto() == expected


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_auto_still_follows_inference_mode_remote(monkeypatch, platform):
    """旧语义不回退：mode=remote（推理在设备 A）→ 转写也在 A，与平台无关。"""
    monkeypatch.setattr(sys, "platform", platform)
    _stub_env(monkeypatch, mode="remote", installed=("mlx_whisper", "faster_whisper"))
    assert B._resolve_auto() == "remote"


# ---- ③ 依赖缺失 / 无 CUDA → AsrProviderUnavailable ----

def test_missing_faster_whisper_raises_unavailable(monkeypatch):
    # sys.modules[name] = None → `import name` 抛 ImportError（模拟没装）
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    with pytest.raises(AsrProviderUnavailable, match="faster-whisper"):
        cuda_asr.transcribe("/tmp/a.wav")


def test_no_cuda_device_raises_unavailable(monkeypatch):
    """无 CUDA 判不可用而不是悄悄退 CPU：CPU 上跑 large-v3-turbo 是"能出结果、慢
    几十倍"的静默故障，fail-open 标记文本才让症状当场可见。"""
    _install_fake_stack(monkeypatch, cuda_devices=0)
    with pytest.raises(AsrProviderUnavailable, match="CUDA"):
        cuda_asr.transcribe("/tmp/a.wav")


def test_broken_ctranslate2_raises_unavailable(monkeypatch):
    _install_fake_stack(monkeypatch)
    monkeypatch.setitem(sys.modules, "ctranslate2", None)
    with pytest.raises(AsrProviderUnavailable, match="ctranslate2"):
        cuda_asr.transcribe("/tmp/a.wav")


def test_ingestion_layer_failopens_on_unavailable(monkeypatch):
    """全链路口径：provider 不可用 → 标记文本，绝不外抛打断整轮对话。"""
    from framework.ingestion.parsers import asr as parser
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    out = parser.transcribe_audio("/tmp/a.wav", provider="faster_whisper")
    assert out.startswith("[Audio transcription unavailable:")


# ---- ④ 模型名映射 ----

@pytest.mark.parametrize("given", [
    None, "", "mlx-community/whisper-large-v3-turbo", B.DEFAULT_ASR_MODEL])
def test_mlx_model_names_map_to_this_legs_default(given):
    """config 的 asr_model 是 MLX 专用件（CTranslate2 读不了它的权重布局），
    一个旋钮服务两条腿 → 由用得着的那条腿翻译。"""
    assert cuda_asr._resolve_model_name(given) == cuda_asr._DEFAULT_MODEL


@pytest.mark.parametrize("given", [
    "large-v3", "Systran/faster-whisper-large-v3", "/data/models/my-ct2-whisper"])
def test_explicit_non_mlx_names_pass_through(given):
    """显式配置永远说了算：CT2 仓库 id / 本地目录 / 内置别名一律原样透传。"""
    assert cuda_asr._resolve_model_name(given) == given


# ---- ⑤ 转写路径纯逻辑 ----

def test_transcribe_consumes_segments_and_autodetects_language(monkeypatch):
    _install_fake_stack(monkeypatch)
    assert cuda_asr.transcribe("/tmp/a.wav") == "你好世界"
    model = _FakeWhisperModel.built[-1]
    assert model.name == cuda_asr._DEFAULT_MODEL
    assert (model.device, model.compute_type) == ("cuda", cuda_asr._COMPUTE_TYPE)
    assert model.download_root.endswith("models"), "权重要收敛在项目数据根内"
    assert model.calls[0]["audio"] == "/tmp/a.wav", "不切区间时整文件直喂"
    assert model.calls[0]["language"] is None, "asr_language: null → 自动检测"


def test_explicit_language_is_forwarded(monkeypatch):
    _install_fake_stack(monkeypatch)
    cuda_asr.transcribe("/tmp/a.wav", language="ja")
    assert _FakeWhisperModel.built[-1].calls[0]["language"] == "ja"


def test_model_is_a_lazy_singleton_per_name(monkeypatch):
    _install_fake_stack(monkeypatch)
    assert not _FakeWhisperModel.built, "未转写前不得加载模型（懒加载）"
    cuda_asr.transcribe("/tmp/a.wav")
    cuda_asr.transcribe("/tmp/b.wav")
    assert len(_FakeWhisperModel.built) == 1, "同名模型必须进程内复用"
    cuda_asr.transcribe("/tmp/b.wav", model="Systran/faster-whisper-large-v3")
    assert len(_FakeWhisperModel.built) == 2, "换模型名则另建一份"


def test_interval_is_sliced_in_memory(monkeypatch):
    """区间切片归 provider（R5-2）。本腿不用 librosa（CUDA 机清单里没有），
    用 faster-whisper 自带的 decode_audio 在内存里切，连临时 wav 都不落。"""
    seen = {}

    def _decode(src, sampling_rate=16000):
        seen["src"] = src
        seen["sr"] = sampling_rate
        return list(range(sampling_rate * 10))          # 10 秒

    _install_fake_stack(monkeypatch, decode=_decode)
    cuda_asr.transcribe("/tmp/a.wav", start=1.0, end=2.5)
    assert seen == {"src": "/tmp/a.wav", "sr": 16000}
    audio = _FakeWhisperModel.built[-1].calls[0]["audio"]
    assert audio == list(range(16000, 40000)), "切片算术须与 mlx 腿逐字一致"


@pytest.mark.parametrize("start,end,expected", [
    (None, 3.0, (0, 48000)),        # 只给 end：从头切到 end
    (5.0, None, (80000, 160000)),   # 只给 start：切到末尾
    (8.0, 2.0, (128000, 128000)),   # 逆序区间：空片，不得抛（fail-open 归调用方）
])
def test_slice_edge_cases(monkeypatch, start, end, expected):
    _install_fake_stack(monkeypatch, decode=lambda src, sampling_rate=16000:
                        list(range(sampling_rate * 10)))
    cuda_asr.transcribe("/tmp/a.wav", start=start, end=end)
    audio = _FakeWhisperModel.built[-1].calls[0]["audio"]
    assert audio == list(range(*expected))
