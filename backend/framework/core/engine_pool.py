"""端点引擎池（混合架构第一波：任务路由决策的执行面）。

main 引擎由 app 装配注入（既有路径零改动）；追加端点（config `inference.endpoints`）
首次被路由命中时**懒装配**：RemoteBackend + 注册表条目的方言与模型级开关，
base_url 经 kv_config.remote 覆盖注入——不动后端与引擎的签名。三条约束：

- **仅 remote 模式可建追加端点**：inprocess 单机单模型是 16GB 开发机的物理约束
  （多模型物理分层留生产机，CHARTER §7 分期纪律）；
- **env 个人环境事实不传专科引擎**：路由目标全是无状态单发（沙箱视觉回读、
  将来的脱敏/升格判定），与沙箱隔离不变量同方向——个人事实不进子生成；
- **装配失败不缓存、响亮上抛**：消费方自行 fail-soft（回 main / 跳过增强步骤），
  池不做静默回落——回落 = 把视觉请求发给无视觉模型，症状比报错难查十倍。
"""
import asyncio
from typing import Optional


class EnginePool:
    """端点名 → AgentEngine。"main" 恒为注入的主引擎，其余按需懒建。"""

    def __init__(self, main_engine, inference_cfg: Optional[dict] = None):
        """inference_cfg 仅供测试注入；缺省（生产）每次懒建时现读 config.yaml
        （与 resolve_endpoints 同款约定，跟随热改的 .env.local 合并结果）。"""
        self._engines = {"main": main_engine}
        self._cfg_arg = inference_cfg
        self._lock = asyncio.Lock()

    async def engine_for(self, name: str):
        """按端点名取引擎；未装配则懒建。未声明的名字响亮 KeyError（base_url_for
        同款立场），inprocess 模式建追加端点响亮 RuntimeError。"""
        eng = self._engines.get(name)
        if eng is not None:
            return eng
        async with self._lock:
            eng = self._engines.get(name)
            if eng is not None:
                return eng
            from ..utils.config_loader import load_section
            from ..utils.model_selection import resolve_endpoints
            cfg = self._cfg_arg if self._cfg_arg is not None else load_section("inference")
            eps = {e["name"]: e for e in resolve_endpoints(self._cfg_arg)}
            ep = eps.get(name)
            if ep is None:
                raise KeyError(f"未声明的推理端点 {name!r}（config inference.endpoints "
                               f"里声明；现有: {sorted(eps)}）")
            if str(cfg.get("mode") or "inprocess") != "remote":
                raise RuntimeError(
                    f"追加推理端点 {name!r} 仅 remote 模式可装配（当前 mode="
                    f"{cfg.get('mode')!r}）：进程内单机单模型是物理约束，"
                    f"多模型常驻需算力端多实例（inference/server.py 多 port）")
            entry = next((e for e in cfg.get("model_registry") or []
                          if e.get("repo") == ep["repo"]), {})
            from ..utils.path_resolver import resolve_tokenizer_only_path
            from .engine import AgentEngine
            model_path = resolve_tokenizer_only_path(ep["repo"], revision=ep.get("revision"))
            print(f"🔀 [EnginePool] 装配专科端点 {name!r}: "
                  f"{str(ep['repo']).split('/')[-1]} @ {ep['host']}:{ep['port']}")
            kv = {**cfg, "remote": {**(cfg.get("remote") or {}),
                                    "host": ep["host"], "port": ep["port"]},
                  # 模型身份护栏（分离部署缺陷二）：专科端点同样核对——那台实例的
                  # /health 真身必须就是本端点声明的 repo。错开时装配响亮上抛；
                  # 消费方按池纪律 fail-soft 回落 main（错开的专科端点缺席增强步骤，
                  # 好过把请求打给错模型静默出垃圾）
                  "expected_model": {"repo": ep["repo"], "revision": ep.get("revision")}}
            eng = await AgentEngine.create(model_path, kv_config=kv, model_options=entry,
                                           env=None, backend="remote", max_concurrency=1)
            self._engines[name] = eng
            return eng

    def shutdown_extras(self) -> None:
        """释放全部追加端点引擎（app lifespan 收尾调用）；main 由 app 自己收。"""
        for name, eng in list(self._engines.items()):
            if name == "main":
                continue
            try:
                eng.backend.shutdown()
            except Exception:
                pass
            self._engines.pop(name, None)
