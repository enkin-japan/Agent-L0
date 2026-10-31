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
  逐 chunk 检查在**首 chunk 之前**（预填期）看不见取消：调用方可经
  features["cancel_scope"]（CancelScope）给后端一个跨线程的中止入口，见下。
"""
import threading
from dataclasses import dataclass, field
from typing import (Any, Callable, Dict, Iterator, List, Optional, Protocol,
                    runtime_checkable)


class CancelScope:
    """一次生成的跨线程取消入口（W-1/C）：调用方（推理服务）持有，后端可选登记中止钩子。

    为什么不是直接 `gen.close()`：生成器正被推理线程 `next()` 着（预填期阻塞在后端
    队列上）时，从别的线程 close 只会抛 `ValueError: generator already executing`。
    所以分两半：取消方（事件循环线程）调 `cancel()` → 同步执行后端登记的钩子（必须
    非阻塞、线程安全，例如 CUDA 后端的 `fut.cancel()` + 桥队列 close，唤醒阻塞的
    `get` 并让私有循环走 `engine.abort`）；推理线程随后自己从 `next()` 返回/抛出，
    再由它自己 close 生成器——生成器只在拥有它的线程上被关。

    语义：`cancel()` 幂等；取消后再登记的钩子当场执行（覆盖「取消先于后端起步」
    的竞态）；钩子异常吞掉（止损路径不得因单个钩子失败而中断）。`add_hook` 返回
    注销函数：生成正常结束时后端须注销，避免收尾时的 cancel 打到已完成的请求上。
    不支持钩子的后端（如 MLX：预填是推理线程里的同步计算，无可中断的等待点）忽略
    此键，取消照旧只在下一个 chunk 边界生效。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._hooks: List[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def add_hook(self, fn: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            run_now = self._cancelled
            if not run_now:
                self._hooks.append(fn)
        if run_now:
            self._run(fn)

        def _remove() -> None:
            with self._lock:
                try:
                    self._hooks.remove(fn)
                except ValueError:
                    pass
        return _remove

    def cancel(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            hooks, self._hooks = self._hooks, []
        for fn in hooks:          # 锁外执行：钩子可能回调 add_hook/_remove
            self._run(fn)

    @staticmethod
    def _run(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 —— 止损路径：记一行，不外抛
            print(f"[CancelScope] 中止钩子异常（已忽略）: {type(e).__name__}: {e}")


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
                  want_delta（True 时后端为 GenChunk.delta 计算展示增量）/
                  cancel_scope（CancelScope，可选：后端若能跨线程中止在飞请求——
                  含首 chunk 前的预填期——就登记钩子；被中止时迭代器以异常结束）。
        """
        ...
