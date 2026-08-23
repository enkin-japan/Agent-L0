"""VLM 家族的**纯文本件**装载（Ornith-1.5-35B-A3B 案，2026-08-24）。

背景：Ornith-1.5-35B-A3B 的 MLX 件沿用 Qwen3.5-MoE 的 `Qwen3_5MoeForConditionalGeneration`
架构名与 `qwen3_5_moe` model_type，但 config 根本没有 `vision_config` 键（等价于 null）、
权重里 0 个 `vision_tower.*` 键（上游只发了文本塔）。mlx_vlm 0.6.13 的 `load_model` 按 model_type 命中原生 VLM 类，
无条件 `VisionModel(config.vision_config)` 建塔，随后 `load_weights(strict=True)` 报
「Missing 393 parameters」。它自带的 `text_only` 包装只在 model_type **不在**
mlx_vlm 注册面时才启用（Qwen3.5 系在，故永远走不到），且双层包装与 mlx_lm 的
`Model→language_model→model` 结构错位（embed_tokens 找不到）——两条现成路都不通。

做法（最窄改动，行为跟人不跟版本）：
- **只对纯文本 config**（`vision_config/audio_config/dflash_config` 全 None/{}）且
  model_type 在 `SUPPORTED` 内的目录生效；其余一概原样交给 `mlx_vlm.utils.load`，
  现有 gemma4 / Qwen3.8 各档装载路径零触碰。
- 模型侧：`get_model_and_args` 在**装载期间**临时替换为返回一个模块代理，代理的
  `Model` 是原生 VLM 类的子类，`__init__` 跳过视觉塔（`vision_tower = None`）、只建
  `language_model`。其余（`ModelConfig/TextConfig/LanguageModel/sanitize/量化谓词`）全
  部沿用原类——文本塔的 per-layer 量化覆写、MoE 权重整形都走上游原路。替换用
  try/finally 复原，不留全局副作用。
- 处理器侧：`AutoProcessor` 会去找 `preprocessor_config.json`（纯文本件没有）而报
  OSError；改为 `AutoTokenizer` + mlx_vlm 同款流式 detokenizer + StoppingCriteria，
  套一层 `TextOnlyProcessor` 给出 `.tokenizer` 属性（引擎 `processor.tokenizer.encode`
  与 mlx_vlm `prepare_inputs` 的 `hasattr(processor, "tokenizer")` 分支都靠它）。

不在本补丁射程：图像输入。注册表 capabilities 只声明 text，但主循环
（engine.py 把 session.images 直接递给 stream_generate）**不按 capabilities 拦图**——
带图请求到了这里会在 mlx_vlm `process_inputs_with_fallback` 处以 ValueError 响亮失败
（没有 image_processor），不会静默出错；按模态挑档是路由层（model_router）的事。

并发：装载期的全局替换用模块级锁串行化（生产装载本就单线程：AgentEngine.create →
单次 _load_all；server lifespan 装一次），锁只是把这个假设变成事实。
"""
from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path
from typing import Any, Optional, Tuple

import mlx.nn as nn

# 已验证的家族：Qwen3.5（稠密）与 Qwen3.5-MoE 的 mlx_vlm 原生类均为
# 「nn.Module.__init__ → vision_tower → language_model」三行构造，子类可安全跳塔。
# 其他家族的构造形态未核对，不擅自扩——宁可在装载时响亮失败。
SUPPORTED = ("qwen3_5_moe", "qwen3_5")
_MODAL_KEYS = ("vision_config", "audio_config", "dflash_config")
_PATCH_LOCK = threading.Lock()


def is_text_only_config(cfg: dict) -> bool:
    """**唯一**判定实现（目录判定与装载期替换共用，防两处口径漂移）：
    model_type（原始值，小写比较）在白名单内，且三个模态键全为 None/{}/缺席
    （与 mlx_vlm `_is_text_only_config` 的「None 或 {} 都算无」同口径）。"""
    if not isinstance(cfg, dict):
        return False
    mt = str(cfg.get("model_type") or "").lower()
    if mt not in SUPPORTED:
        return False
    return all(not cfg.get(k) for k in _MODAL_KEYS)


def _read_config(model_path: Path) -> Optional[dict]:
    cfg_path = Path(model_path) / "config.json"
    if not cfg_path.is_file():
        return None
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_text_only_dir(model_path) -> bool:
    """目录是否为「受支持家族的纯文本件」（读 config.json 后交给 is_text_only_config）。"""
    if model_path is None:
        return False
    cfg = _read_config(Path(model_path))
    return bool(cfg) and is_text_only_config(cfg)


class _ArchShim:
    """模块代理：`Model` 换成跳塔子类，其余属性透传到原生架构模块。"""

    def __init__(self, mod):
        self._mod = mod
        base = mod.Model
        lm_cls = mod.LanguageModel

        class Model(base):  # type: ignore[misc,valid-type]
            def __init__(self, config):
                nn.Module.__init__(self)
                self.config = config
                self.vision_tower = None
                self.language_model = lm_cls(config.text_config, config)

        Model.__name__ = base.__name__ + "TextOnly"
        Model.__qualname__ = Model.__name__
        self.Model = Model

    def __getattr__(self, name):
        return getattr(self._mod, name)


@contextlib.contextmanager
def _text_only_arch():
    import mlx_vlm.utils as U

    with _PATCH_LOCK:          # 串行化替换窗口：重入/并发装载不会把补丁永久留在模块里
        orig = U.get_model_and_args

        def patched(config: dict):
            arch, model_type = orig(config)
            # 用原始 config 判（与 is_text_only_dir 同一实现），不看 MODEL_REMAPPING 后的名字
            if is_text_only_config(config):
                return _ArchShim(arch), model_type
            return arch, model_type

        U.get_model_and_args = patched
        try:
            yield
        finally:
            U.get_model_and_args = orig


class TextOnlyProcessor:
    """给纯文本件的「处理器」：只有分词器。

    - `.tokenizer`：引擎与 mlx_vlm 都先探这个属性；
    - `__call__`：mlx_vlm `prepare_inputs` 在无图/无音频分支直接调用 `tokenizer(...)`，
      有 `.tokenizer` 时用的是它而非本对象，这里一并给出只为脚本直连场景；
    - `image_processor = None`：显式占位，免得 `__getattr__` 透传到分词器上抛
      AttributeError（有代码 getattr 探它）；
    - 其余属性（eos_token、pad_token、apply_chat_template…）透传分词器。
    """

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
        self.image_processor = None
        self.detokenizer = None

    def __call__(self, *args, **kwargs):
        return self.tokenizer(*args, **kwargs)

    def __getattr__(self, name):
        # 仅在常规属性查找失败后触发；self.__dict__ 内的不会走到这里。
        # dunder 不透传（copy/pickle 探测 __reduce_ex__/__getstate__ 之类要拿到 AttributeError
        # 才会走缺省路径）；__init__ 未跑完（反序列化半成品）时 tokenizer 不在 __dict__，
        # 直接抛而不是再进 __getattr__ 无限递归。
        if name.startswith("__") or "tokenizer" not in self.__dict__:
            raise AttributeError(name)
        return getattr(self.tokenizer, name)


_UNSUPPORTED_LOAD_KWARGS = ("adapter_path", "revision")


def _resolve_eos(model, tokenizer):
    """与 mlx_vlm.load_processor 同梯度：模型 config 的 eos（可为列表）→ tokenizer.eos_token_ids
    → tokenizer.eos_token_id；全空响亮失败（StoppingCriteria 收 None 只会在首 token 处炸）。"""
    for cand in (getattr(model.config, "eos_token_id", None),
                 getattr(tokenizer, "eos_token_ids", None),
                 getattr(tokenizer, "eos_token_id", None)):
        if cand is None or cand == []:
            continue
        return cand
    raise ValueError("纯文本件装载：模型 config 与分词器都没有 eos_token_id，无法构造停止判据")


def load_text_only(model_path, **kwargs) -> Tuple[Any, TextOnlyProcessor]:
    """纯文本件的 (model, processor)，与 `mlx_vlm.utils.load` 同返回形态。

    调用前提：`is_text_only_dir(model_path)` 为真（`load_model_and_processor` 已判过）。
    `adapter_path`/`revision` 这两个 `mlx_vlm.load` 的关键字在本路径无实现：传入即拒，
    不做「看起来接受了其实没生效」的假兼容。
    """
    import mlx_vlm.utils as U
    from mlx_vlm.tokenizer_utils import load_tokenizer
    from transformers import AutoTokenizer

    bad = [k for k in _UNSUPPORTED_LOAD_KWARGS if kwargs.get(k) is not None]
    if bad:
        raise ValueError(f"纯文本件装载不支持关键字 {bad}（本补丁不做 LoRA/远端 revision）")
    for k in _UNSUPPORTED_LOAD_KWARGS:
        kwargs.pop(k, None)

    model_path = Path(model_path)
    with _text_only_arch():
        model = U.load_model(model_path, **kwargs)

    tokenizer = AutoTokenizer.from_pretrained(model_path)
    processor = TextOnlyProcessor(tokenizer)
    # 与 mlx_vlm.load_processor(add_detokenizer=True) 同款接线：流式 detokenizer 按
    # tokenizer.json 的 decoder 类型推断；停止判据按同一优先级梯度取 eos。
    detok_cls = load_tokenizer(model_path, return_tokenizer=False)
    processor.detokenizer = detok_cls(tokenizer)
    tokenizer.stopping_criteria = U.StoppingCriteria(_resolve_eos(model, tokenizer), tokenizer)
    return model, processor


def load_model_and_processor(model_path, **kwargs):
    """统一入口：纯文本件走本补丁，其余原样走 `mlx_vlm.utils.load`。"""
    if is_text_only_dir(model_path):
        print(f"📝 text-only VLM-family checkpoint detected, loading without vision tower: "
              f"{Path(model_path).name}")
        return load_text_only(model_path, **kwargs)
    from mlx_vlm.utils import load as _vlm_load
    return _vlm_load(model_path, **kwargs)
