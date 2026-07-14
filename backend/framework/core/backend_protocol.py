"""推理后端协议（HETERO_COMPUTE P0）：智能层与物理推理承载之间的唯一边界。

AgentEngine（core/engine.py）持有一个 InferenceBackend 实例；后端负责模型加载、
KV 缓存生命周期与逐 token 生成流。协议对上只承诺行为语义，不承诺实现——
进程内 MLX、进程内 CUDA（A 线）与远程服务（B 线）都是它的实现，
全部住在 backends/ 之下（目录约定见 HETERO_COMPUTE §D D7）。

设计约定（与既有语义逐条对齐，P0 零行为变化的依据）：
- clone_cache 返回 None = 不支持/失败，调用方回退冷启动预填充（既有分支复用语义）；
- stream_generate 是**同步迭代器**，跑在 AgentEngine 的专用推理执行器线程里，
  推理互斥锁 / call_soon_threadsafe 回主循环等线程纪律全部留在 AgentEngine；
- 取消协作：调用方逐 chunk 检查取消标志后 break，生成器被弃置即停。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Protocol, runtime_checkable


@dataclass
class GenChunk:
    """stream_generate 的逐段产出。

    text  规范文本段：全程累加即为本次生成的完整原始文本（含频道/工具标记）。
    delta 展示增量：供真流式过滤器消费的可见文本增量（后端若能做 detokenizer 级
          差分则给出精确增量；None 表示后端无差分能力，调用方退回用 text）。
    extra 后端附加仪表（如投机采样 spec_drafted/spec_accepted），无则空 dict。
    """
    text: str
    delta: Optional[str] = None
    prompt_tokens: int = 0
    generation_tokens: int = 0
    prompt_tps: float = 0.0
    generation_tps: float = 0.0
    peak_memory: float = 0.0
    extra: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CacheHandle(Protocol):
    """不透明的会话级推理缓存句柄。对上层只承诺身份与生命周期，不承诺实现。

    MLX 实现 = 包一层 PromptCacheState；远程实现 = 服务端注册表里的 id。
    token_ids 只读暴露：前缀匹配裁剪的依据（无缓存内容时为 None）。
    """
    @property
    def token_ids(self) -> Optional[List[int]]: ...


class InferenceBackend(Protocol):
    """推理后端协议。除 load 外全部为同步方法（在推理执行器线程内被调）。"""

    @classmethod
    async def load(cls, model_path, **cfg) -> "InferenceBackend":
        """加载模型并装配后端。cfg 透传后端专属配置（含可选 executor：
        进程内后端须在该执行器线程内加载，保证推理宇宙同线程）。"""
        ...

    def new_cache(self) -> CacheHandle:
        """新建一份空的会话缓存。"""
        ...

    def clone_cache(self, src: CacheHandle) -> Optional[CacheHandle]:
        """分支复用：克隆出物理独立的缓存。返回 None = 不支持/失败，
        调用方回退冷启动预填充（既有语义，正确性不受影响）。"""
        ...

    def release_cache(self, h: CacheHandle) -> None:
        """冻结/删除会话时释放缓存占用的物理资源（幂等）。"""
        ...

    def shutdown(self) -> None:
        """进程退出前释放后端持有的全局资源。"""
        ...

    def stream_generate(self, prompt: str, *,
                        cache: CacheHandle,
                        images: Optional[List[str]] = None,
                        audios: Optional[List[str]] = None,
                        sampling: Optional[Dict[str, Any]] = None,
                        features: Optional[Dict[str, Any]] = None,
                        ) -> Iterator[GenChunk]:
        """单次生成的同步迭代器（跑在推理执行器线程），逐 token 段产出 GenChunk。

        sampling: temperature / min_p / max_tokens / repetition_penalty /
                  repetition_context_size。
        features: enable_thinking / thinking_budget / thinking_start_token /
                  thinking_end_token（预算锚点，缺省不启用预算裁决）/
                  tool_guard（core.constrained.ToolCallGuard 纯逻辑状态机，
                  后端负责把它适配进自己的 logits 处理协议）/
                  want_delta（True 时后端为 GenChunk.delta 计算展示增量）。
        """
        ...
