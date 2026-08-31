"""远程推理后端：InferenceBackend 协议的 HTTP/SSE 客户端（HETERO_COMPUTE B1/D2）。

设备 B（执行层）经本后端把生成请求发给设备 A 的 inference-server（:14161，
服务面即 §1.1 协议的 SSE 化）。会话数据不出设备 B：服务端只见 prompt 与媒体，
不持久化会话（隐私边界见 HETERO_COMPUTE §3.1）。

与进程内后端的语义对齐：
- 分词器在本地加载（纯 transformers，不装 MLX/权重也可用——只需模型目录里的
  tokenizer 文件）：PromptBuilder / ResponseParser / 约束 guard / token 分解仪表
  全是设备 B 资产，prompt 以字符串跨网，两侧 tokenizer 同源（同一模型目录）。
- 缓存句柄 = 服务端注册表 id，**懒注册**：new_cache() 不发网络请求（会话创建在
  事件循环线程，不能阻塞），首次生成时服务端分配 id 并经 meta 事件回填。
  服务端 45min TTL 兜底泄漏（客户端崩溃/沙箱一次性缓存未显式释放）。
- clone_cache 走 /v1/cache/{id}/clone；失败/未注册返回 None → 调用方冷启动（既有语义）。
- 约束采样：纯逻辑 guard 无法跨网逐 token 驱动，改为把工具声明（features.tool_specs，
  引擎侧随 guard 一并给出）发给服务端、服务端用自己的约束引擎重建 guard；
  done 事件把 constrained_steps 回填进本地 guard.masked_positions（仪表口径不变）。
- 展示增量：服务端做 detokenizer 差分，chunk.delta 原样透传（中文逐 token 流式不降级）。
- 网络错误/超时 → RuntimeError 上抛，走既有引擎故障路（整轮 fail 落盘 + 失败可见）。
- 取消：引擎逐 chunk 检查取消标志后弃置迭代器 → 生成器关闭时 best-effort 发
  /v1/generate/{gen_id}/cancel 并断开 SSE 连接（服务端对断连同样止损）。
"""
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import requests

from ...core.backend_protocol import GenChunk

# 逐 chunk 读超时要容纳最坏的冷启动 prefill（长会话冷恢复在弱机上可达分钟级）
_CONNECT_TIMEOUT = 5.0
_STREAM_READ_TIMEOUT = 300.0
_CONTROL_TIMEOUT = 15.0     # 缓存控制面（clone 含服务端深拷贝，稍宽）
_PERSIST_TIMEOUT = 30.0     # KV 落盘/恢复（服务端序列化百 MB 级文件，再宽一档）
_RELEASE_TIMEOUT = 5.0      # 释放走事件循环线程（freeze 是同步路径），必须短；TTL 兜底
_MEDIA_CACHE_FRESH_SECONDS = 3600.0   # 上传缓存鲜度：远小于服务端 media TTL（≥1 天），
                                      # 量级差即无「刚确认就被清」竞态窗


def guard_model_identity(ident: dict, expected: dict, base_url: str) -> None:
    """模型身份护栏（分离部署缺陷二）：服务端 /health 报的真身 vs 本侧期望的选型。

    两侧的模型选择各自落地（算力端 .model-selection / 执行端 .model-selection 或
    config 默认），没有比对时错开的症状是**分词器与方言驱动错权重静默出垃圾**——
    比 401 难查十倍。范式同 RAG 档位护栏（rag_server._guard_or_stamp_store）：
    配置性错误装配期响亮拒启，不留到首轮生成。判定序：
      - service 报了别的名字 → 拒启（端口被外来程序占用；repo 恰好对上也不放行）；
      - 服务端未报身份（旧版 server / 外来程序）→ 拒启（fail-closed：无法核对 =
        不核对，护栏会被版本错峰静默架空）；
      - repo 不符 → 拒启，指明两侧各自怎么改；
      - revision 两侧都有且不符 → 拒启（同 repo 不同钉扎也是错峰）；单侧缺省不比
        （服务端可能按注册表解析了默认 revision，而本侧未钉扎）；
      - 全符 → 放行并打印核对结论。
    expected 为空（无 "repo"）时整个跳过——直接构造/测试路径的既有行为不变。
    ident 非 dict（外来 http 服务回 JSON 标量/数组）按「未报身份」处理，不许
    AttributeError 把最需要可读性的报错糊成裸 traceback（首评 S3）。
    """
    if not (expected or {}).get("repo"):
        return
    if not isinstance(ident, dict):
        ident = {}
    svc = ident.get("service")
    if svc and svc != "macagent-inference":
        raise RuntimeError(
            f"{base_url} 上不是 inference-server（service={svc!r}）：端口被外来程序占用。\n"
            f"  检查算力端该端口上跑的是什么，或修正 config inference.remote 的 host/port。")
    got_repo = ident.get("model_repo")
    if not got_repo:
        raise RuntimeError(
            f"inference-server（{base_url}）未报模型身份，无法核对两侧选型：旧版 "
            f"inference-server（算力端 git pull 后重启），或端口被外来程序占用。\n"
            f"  护栏拒绝盲信本地选型继续启动（分离部署缺陷二：错开=静默出垃圾）。")
    if got_repo != expected["repo"]:
        raise RuntimeError(
            f"模型档位错开：算力端在跑 {got_repo}，本侧选型是 {expected['repo']}。\n"
            f"  两侧必须同档。dev 形态：任一侧 ./devctl.sh model 对齐；app 形态：本侧\n"
            f"  改不了选型，唯一补救在算力端——把算力端换回 app 打包默认档再重启本 app。")
    exp_rev, got_rev = expected.get("revision"), ident.get("model_revision")
    if exp_rev and got_rev and exp_rev != got_rev:
        raise RuntimeError(
            f"模型 revision 错开（repo 同为 {got_repo}）：算力端 {got_rev}，本侧 {exp_rev}。\n"
            f"  两侧 config.yaml model_registry 的钉扎不一致——先 git pull 对齐再启动。")
    print(f"[RemoteBackend] 模型身份核对通过：{got_repo}"
          f"（revision={got_rev or '未钉扎'}，capabilities={ident.get('capabilities')}）")


class _ProcessorShim:
    """把裸 tokenizer 包成引擎期望的 processor 形状（.tokenizer 属性）。

    AutoProcessor 对本模型目录不可用（缺视频预处理配置），而 PromptBuilder /
    ResponseParser / 约束引擎 / 仪表只消费 processor.tokenizer，裸 shim 即够。
    """
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer


class RemoteCacheHandle:
    """CacheHandle 的远程实现：服务端缓存注册表里的 id（懒分配，见模块头）。"""
    __slots__ = ("cache_id",)

    def __init__(self, cache_id: Optional[str] = None):
        self.cache_id = cache_id

    @property
    def token_ids(self) -> Optional[List[int]]:
        return None  # 前缀轨迹在服务端句柄内部，客户端不持有


class RemoteBackend:
    """InferenceBackend 的远程实现（HTTP/SSE 客户端）。"""

    @classmethod
    async def load(cls, model_path, *, executor=None, **cfg) -> "RemoteBackend":
        """本地只加载分词器（同步、轻量，无线程绑定问题，executor 不使用）。"""
        from transformers import AutoTokenizer
        remote = cfg.get("remote") or {}
        host = str(remote.get("host", "127.0.0.1"))
        port = int(remote.get("port", 14161))
        processor = _ProcessorShim(AutoTokenizer.from_pretrained(str(model_path)))
        backend = cls(processor, base_url=f"http://{host}:{port}")
        # 多端点表（接口准备③）：主端点沿用上面 cfg 推导（零回归），追加端点来自
        # config inference.endpoints（resolve_endpoints 已在 app 装配期校验过，这里
        # 出错说明装配序被破坏——照样响亮抛，不兜）。生成路径仍走 main；专科调用
        # （多模型协作）将来经 base_url_for(name) 取址。
        from framework.utils.model_selection import resolve_endpoints
        for ep in resolve_endpoints()[1:]:
            backend._endpoint_urls[ep["name"]] = f"http://{ep['host']}:{ep['port']}"
        # 就绪探活：装配期即暴露"服务端不可达"（否则要到首轮生成才炸，定位更绕）
        try:
            r = backend._session.get(f"{backend.base_url}/health", timeout=_CONNECT_TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            raise RuntimeError(
                f"inference-server 不可达（{backend.base_url}）：{e}\n"
                f"  请先启动推理服务（inference/server.py）或把 config inference.mode 改回 inprocess"
            ) from e
        # 模型身份护栏（分离部署缺陷二）：期望身份由装配方注入 cfg.expected_model
        # （app.py 主端点 / engine_pool 专科端点），服务端真身取自刚拿到的 /health。
        # 注意放在可达性 try 之外：档位错开是配置性错误，不得被包装成「不可达」。
        expected = cfg.get("expected_model") or {}
        if expected.get("repo"):
            try:
                ident = r.json()
            except ValueError:
                ident = {}
            guard_model_identity(ident, expected, backend.base_url)
        print(f"[RemoteBackend] 已连接 inference-server: {backend.base_url}")
        return backend

    def __init__(self, processor, base_url: str):
        self.model = None          # 远程模式设备 B 不持有权重（装配面 app.state.model 为 None）
        self.processor = processor
        self.base_url = base_url.rstrip("/")
        # 端点名 → 拨号地址（load 时按 config inference.endpoints 追加；直接构造=单端点）
        self._endpoint_urls = {"main": self.base_url}
        # 密钥与 backend 侧惯例一致走环境变量（.env.local 注入，不进仓库）
        self._api_key = os.environ.get("INFER_API_KEY", "")
        self._session = requests.Session()
        if self._api_key:
            self._session.headers["Authorization"] = f"Bearer {self._api_key}"
        # 本进程已确认上传的 media_id → 确认时刻（sha256 内容寻址）。带时间戳是因为
        # 服务端 media/ 有 TTL 清扫（缺陷三）：backend 是长命进程，光记「传过」会在
        # 服务端清掉后拿着幻影 id 发生成请求——服务端只会「缺媒体跳过」，症状是图
        # 静默不见。过期条目先 HEAD 探活（顺带给服务端 TTL 续命），404 才重传字节。
        self._uploaded_media: Dict[str, float] = {}

    def base_url_for(self, endpoint: str = "main") -> str:
        """按端点名取拨号地址（多模型协作的专科调用消费面，接口准备③）。

        未声明的名字响亮 KeyError 并列出现有端点——拼错端点名是配置/调用方错误，
        不得静默回落 main（回落=把视觉请求发给无视觉的主模型，症状比报错难查十倍）。
        """
        try:
            return self._endpoint_urls[endpoint]
        except KeyError:
            raise KeyError(
                f"未声明的推理端点 {endpoint!r}（config inference.endpoints 里声明；"
                f"现有：{sorted(self._endpoint_urls)}）") from None

    # ---- 缓存生命周期 ----
    def new_cache(self) -> RemoteCacheHandle:
        return RemoteCacheHandle()  # 懒注册：首次生成时服务端分配 id

    def clone_cache(self, src) -> Optional[RemoteCacheHandle]:
        cid = getattr(src, "cache_id", None)
        if not cid:
            return None  # 源会话从未在服务端生成过 → 冷启动
        try:
            r = self._session.post(f"{self.base_url}/v1/cache/{cid}/clone",
                                   timeout=_CONTROL_TIMEOUT)
            if r.status_code != 200:
                return None  # 501=不支持 / 404=已被 TTL 回收 → 冷启动回退（既有语义）
            new_id = r.json().get("cache_id")
            return RemoteCacheHandle(new_id) if new_id else None
        except Exception as e:
            print(f"[RemoteBackend] 缓存克隆失败，分支将冷启动: {e}")
            return None

    # ---- KV 冻结落盘（T2/Q9-a：config inference.kv_persist 总开关在服务端后端生效）----
    # dest 是边端会话本地路径（sessions/<sid>/kv_cache.safetensors）；远端存储键取其
    # 父目录名（= session_id，会话目录命名约定）——服务端按键落 kvstore/<sid>.safetensors。

    @staticmethod
    def _persist_hint(dest: str) -> str:
        return Path(dest).resolve().parent.name

    def persist_cache(self, h, dest: str) -> bool:
        cid = getattr(h, "cache_id", None)
        if not cid:
            return False    # 从未在服务端生成过：无物可落
        try:
            r = self._session.post(f"{self.base_url}/v1/cache/{cid}/persist",
                                   json={"hint": self._persist_hint(dest)},
                                   timeout=_PERSIST_TIMEOUT)
            return r.status_code == 200 and bool(r.json().get("persisted"))
        except Exception as e:
            print(f"[RemoteBackend] KV 落盘失败（解冻走冷路）: {e}")
            return False

    def restore_cache(self, dest: str):
        try:
            r = self._session.post(f"{self.base_url}/v1/cache/restore",
                                   json={"hint": self._persist_hint(dest)},
                                   timeout=_PERSIST_TIMEOUT)
            if r.status_code != 200:
                return None   # 404=无产物/开关关 → 基座/冷路（既有语义）
            cid = r.json().get("cache_id")
            return RemoteCacheHandle(cid) if cid else None
        except Exception as e:
            print(f"[RemoteBackend] KV 热恢复失败（退基座/冷路）: {e}")
            return None

    def drop_persisted(self, dest: str) -> None:
        try:
            self._session.delete(
                f"{self.base_url}/v1/cache/persisted/{self._persist_hint(dest)}",
                timeout=_CONTROL_TIMEOUT)
        except Exception:
            pass              # 服务端文件可随时清空，清理失败无害

    def release_cache(self, h) -> None:
        cid = getattr(h, "cache_id", None)
        if not cid:
            return
        h.cache_id = None
        try:
            self._session.delete(f"{self.base_url}/v1/cache/{cid}", timeout=_RELEASE_TIMEOUT)
        except Exception:
            pass  # 服务端 TTL 兜底

    def shutdown(self) -> None:
        self._session.close()

    # ---- 媒体（内容寻址上传，sha256 去重；服务端 TTL 清扫见 inference media_ttl_days） ----

    def _ensure_media(self, path: str) -> Optional[str]:
        """把本地媒体文件上传到服务端（新鲜确认过的跳过），返回 media_id；失败返回 None
        （该媒体缺席本次生成——与本地文件丢失时 mlx_vlm 的容错方向一致，不炸整轮）。"""
        try:
            p = Path(path)
            data = p.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            media_id = digest + p.suffix.lower()
            # 墙钟而非 monotonic（首评 S）：服务端 TTL 是墙钟 mtime，而 macOS 的
            # monotonic 睡眠不走——长命 backend 睡过 7 天醒来会自以为缓存「不到 1h」，
            # 恰好复活幻影 id 环。墙钟跳变的代价只是多一次 HEAD，方向安全。
            now = time.time()
            confirmed_at = self._uploaded_media.get(media_id)
            if confirmed_at is not None and now - confirmed_at < _MEDIA_CACHE_FRESH_SECONDS:
                return media_id
            # 冷缓存（进程重启）或过期：都先 HEAD 问一声（服务端命中即续 TTL 并零字节
            # 返回）——不这样，backend 每次重启都把会话摸过的百 MB 音频整份重推一遍
            # （首评 S）。404/探活失败才重传字节。
            try:
                hr = self._session.head(f"{self.base_url}/v1/media/{media_id}",
                                        timeout=_CONNECT_TIMEOUT)
                if hr.status_code == 200:
                    self._uploaded_media[media_id] = now
                    return media_id
            except Exception:
                pass   # 探活失败按「不在」处理，走重传路
            self._uploaded_media.pop(media_id, None)
            r = self._session.post(
                f"{self.base_url}/v1/media",
                files={"file": (p.name, data)},
                data={"sha256": digest},
                timeout=(_CONNECT_TIMEOUT, 60.0),
            )
            r.raise_for_status()
            media_id = r.json()["media_id"]
            self._uploaded_media[media_id] = now
            return media_id
        except Exception as e:
            print(f"[RemoteBackend] 媒体上传失败（本次生成将缺席该媒体）: {path}: {e}")
            return None

    # ---- 生成 ----
    def stream_generate(self, prompt: str, *, cache,
                        images: Optional[List[str]] = None,
                        audios: Optional[List[str]] = None,
                        sampling: Optional[Dict[str, Any]] = None,
                        features: Optional[Dict[str, Any]] = None,
                        ) -> Iterator[GenChunk]:
        features = dict(features or {})
        # 纯逻辑 guard 不跨网：留本地引用收仪表回填，线上传工具声明由服务端重建
        guard = features.pop("tool_guard", None)

        body = {
            "cache_id": getattr(cache, "cache_id", None),
            "prompt": prompt,
            "images": [m for m in (self._ensure_media(p) for p in (images or [])) if m],
            "audios": [m for m in (self._ensure_media(p) for p in (audios or [])) if m],
            "sampling": sampling or {},
            "features": features,
        }

        gen_id: Optional[str] = None
        finished = False
        resp = None
        try:
            resp = self._session.post(
                f"{self.base_url}/v1/generate", json=body, stream=True,
                timeout=(_CONNECT_TIMEOUT, _STREAM_READ_TIMEOUT),
            )
            if resp.status_code != 200:
                raise RuntimeError(f"inference-server 拒绝生成请求: "
                                   f"HTTP {resp.status_code} {resp.text[:300]}")
            event = ""
            for raw in resp.iter_lines():
                line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                if line.startswith("event:"):
                    event = line[len("event:"):].strip()
                    continue
                if not line.startswith("data:"):
                    continue  # 空行=事件边界；其余行忽略
                data = json.loads(line[len("data:"):].strip() or "{}")
                if event == "meta":
                    gen_id = data.get("gen_id")
                    # 采纳服务端回执的生效 cache_id：懒注册首分配、以及服务端重启/TTL
                    # 回收后换发的新 id（旧 id 失效时服务端冷启动换新，回填后恢复热复用）
                    if data.get("cache_id"):
                        cache.cache_id = data["cache_id"]
                elif event == "chunk":
                    yield GenChunk(
                        text=data.get("text", ""),
                        delta=data.get("delta"),
                        prompt_tokens=data.get("prompt_tokens", 0),
                        generation_tokens=data.get("generation_tokens", 0),
                        prompt_tps=data.get("prompt_tps", 0.0),
                        generation_tps=data.get("generation_tps", 0.0),
                        peak_memory=data.get("peak_memory", 0.0),
                        extra=data.get("extra") or {},
                    )
                elif event == "done":
                    finished = True
                    # 约束仪表回填：服务端 guard 的受约束步数落回本地 guard（口径不变）
                    if guard is not None and data.get("constrained_steps"):
                        guard.masked_positions = int(data["constrained_steps"])
                    break
                elif event == "error":
                    raise RuntimeError(f"inference-server 生成失败: {data.get('message', '未知错误')}")
            # 流在 done/error 之前干净结束 = 服务端中断（进程被杀时 TCP FIN 不算异常，
            # iter_lines 只会安静 EOF）。必须显式报错：静默返回会让上层拿到"空生成"
            # 继续走解析重试，轮次状态被污染（实测服务端拔线时的病理）。
            if not finished:
                raise RuntimeError(
                    f"inference-server SSE 流在生成完成前中断（{self.base_url}，服务端可能已退出）")
        except requests.RequestException as e:
            raise RuntimeError(f"inference-server 网络异常（{self.base_url}）: {e}") from e
        finally:
            # 未走到 done 即离场（上层取消弃置迭代器 / 中途异常）：显式取消服务端生成，
            # 再断开 SSE——服务端对二者都会在下一个 token 边界停止消耗算力
            if not finished and gen_id:
                try:
                    self._session.post(f"{self.base_url}/v1/generate/{gen_id}/cancel",
                                       timeout=_RELEASE_TIMEOUT)
                except Exception:
                    pass
            if resp is not None:
                resp.close()
