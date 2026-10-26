"""Compute availability for the HTTP adapter, without owning any turn lifecycle."""
import asyncio
import re
import time
from contextlib import suppress

import httpx

# A single lost probe cycle (tailnet hiccup, inference-server restart) must not take
# execution away from the user; three consecutive cycles is a real outage.
MONITOR_FAILURES_BEFORE_UNAVAILABLE = 3
MESSAGE_MAX_CHARS = 300
# An outage that lasts hours must not print a line every probe cycle (5 s) or every
# retry (≤30 s): a state change is logged at once, the same state at most this often.
LOG_REPEAT_SECONDS = 300
# The guidance the shell launcher used to print before it stopped gating startup
# (scripts/lib_edge.sh require_host / probe_auth): the operator needs to know what
# to go fix, and after this change the backend log is the only place that says it.
HOST_GUIDANCE = ("算力端地址为空——app 形态：设置 → 执行层 → 算力端地址；"
                 "dev 形态：.env.local 写 INFER_REMOTE_HOST=<算力端 IP>；"
                 "单机部署请改 inference.mode: inprocess")
INFER_KEY_GUIDANCE = ("算力端拒绝鉴权——INFER 密钥缺失或不对：须与算力端 .env.local 的"
                      "INFER_API_KEY 同值、不含引号")
RAG_KEY_GUIDANCE = ("RAG 数据面拒绝鉴权——RAG 密钥缺失或不对：须与数据面 .env.local 的"
                    "RAG_API_KEY 同值、不含引号")
_HOST_MARKERS = ("算力端地址为空", "INFER_REMOTE_HOST", "http://:", "https://:")
_RAG_PATHS = ("/api/v1/category",)


_BEARER = re.compile(r"(?i)bearer\s+[^\s'\"]+")


def describe_failure(exc) -> str:
    """One sanitized line of the exception, plus the actionable guidance it implies.

    Startup failures come from httpx, guard_model_identity and config validation.
    Only the exception's own first line is reported: enough to tell a refused
    connection from a wrong model revision, while a multi-line httpx hint or a
    huge upstream body cannot flood the log. Request headers are deliberately
    never added here — the Bearer key must not reach a log line, and httpx keeps
    headers out of its own messages.
    """
    first = next((line.strip() for line in str(exc).splitlines() if line.strip()), "")
    # 兜底：非法字符的密钥会让 h11 抛出带整行头值的 LocalProtocolError
    first = _BEARER.sub("Bearer <redacted>", first)
    if len(first) > MESSAGE_MAX_CHARS:
        first = first[:MESSAGE_MAX_CHARS - 1] + "…"
    guidance = _guidance_for(exc, first)
    return f"{first}｜{guidance}" if guidance else first


def _guidance_for(exc, message: str):
    if any(marker in message for marker in _HOST_MARKERS):
        return HOST_GUIDANCE
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        url = str(getattr(getattr(exc, "request", None), "url", "") or "")
        return RAG_KEY_GUIDANCE if any(p in url for p in _RAG_PATHS) else INFER_KEY_GUIDANCE
    return None


async def finish_task(task):
    """Cancellation cannot orphan a load/cleanup worker or permit a second engine."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


async def blocking_call(fn, *args, **kwargs):
    """Keep blocking startup IO off the HTTP loop; join it before cancelling."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await finish_task(task)
        raise


async def probe_remote_compute(base_url, expected, api_key, previous_identity=None):
    """Both the identity and an authenticated read must pass before execution."""
    from ..backends.remote.backend import guard_model_identity

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        health = await client.get(f"{base_url}/health")
        health.raise_for_status()
        identity = health.json()
        if not isinstance(identity, dict) or identity.get("status") != "ok":
            raise RuntimeError("Inference health not confirmed")
        # Reuse the established guard. Avoid repeating its success log for unchanged
        # identities, but always authenticate again (keys can change on restart).
        if previous_identity is None or identity != previous_identity:
            guard_model_identity(identity, expected, base_url)
        # raise_for_status is the authentication/readiness assertion (401/403 raise).
        auth = await client.get(f"{base_url}/metrics", headers=headers)
        auth.raise_for_status()
    return identity


async def probe_rag_data(cfg, api_key):
    """The existing RAG prerequisite requires a positive authenticated response."""
    host = str(cfg.get("host") or "127.0.0.1")
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.get(
            f"http://{host}:{int(cfg.get('port', 14160))}/api/v1/category", headers=headers)
        response.raise_for_status()
        if not isinstance(response.json(), list):
            raise RuntimeError("RAG data authentication/readiness not confirmed")


class ComputeLifecycle:
    """One assembly attempt at a time, followed by availability checks of that engine."""

    def __init__(self, app, assemble, publish, cleanup, probe=None, *,
                 retry_initial=2, retry_max=30, monitor_interval=5, clock=time.monotonic):
        self.app = app
        self.assemble = assemble
        self.publish = publish
        self.cleanup = cleanup
        self.probe = probe
        self.retry_initial = max(0.01, retry_initial)
        self.retry_max = max(self.retry_initial, retry_max)
        self.monitor_interval = max(0.01, monitor_interval)
        self.resources = None
        self.task = None
        self.clock = clock
        self._log_key = None
        self._log_at = 0.0
        self._log_suppressed = 0
        self.app.state.compute_ready = False
        self.app.state.compute_state = "starting"
        self.app.state.compute_installed = False

    async def _install(self):
        resources = await self.assemble()
        # No await between installation and ready: requests cannot see partial deps.
        self.resources = resources
        try:
            self.publish(resources)
        except BaseException:
            self.resources = None
            try:
                await finish_task(asyncio.create_task(self.cleanup(resources)))
            finally:
                # finish_task deliberately joins cleanup despite cancellation.
                # Preserve a stop received during that join: otherwise the bare
                # raise restores the publish error and _run starts another load.
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
            raise
        self.app.state.compute_installed = True
        self.app.state.compute_ready = True
        self.app.state.compute_state = "ready"

    def start(self):
        self.task = asyncio.create_task(self._run(), name="compute-startup")
        return self.task

    @staticmethod
    def _diagnose(exc) -> str:
        detail = describe_failure(exc)
        return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__

    def _log(self, key, line: str) -> None:
        """A state change prints at once; the same state at most every LOG_REPEAT_SECONDS.

        The key carries the failure detail, so a refused connection becoming a 401 is
        a change and is printed immediately; an unchanged outage is not repeated every
        few seconds, burying the one line that changed.
        """
        now = self.clock()
        if key == self._log_key and now - self._log_at < LOG_REPEAT_SECONDS:
            self._log_suppressed += 1
            return
        if key == self._log_key and self._log_suppressed:
            line += f"（同一状态已持续，{LOG_REPEAT_SECONDS // 60} 分钟内省略 {self._log_suppressed} 行）"
        self._log_key, self._log_at, self._log_suppressed = key, now, 0
        print(line)

    async def _run(self):
        delay = self.retry_initial
        while self.resources is None:
            try:
                await self._install()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.app.state.compute_ready = False
                self.app.state.compute_state = "retrying"
                diagnosis = self._diagnose(exc)
                self._log(("retrying", diagnosis),
                          f"[Compute] 计算装配未就绪（{diagnosis}），{delay:g}s 后重试")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.retry_max)
        failures = 0
        while self.probe is not None:
            await asyncio.sleep(self.monitor_interval)
            try:
                await self.probe()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                diagnosis = self._diagnose(exc)
                # Hysteresis: only consecutive failures mean the compute side is
                # gone. Flipping on the first one would 503 a user mid-conversation
                # for a single dropped packet.
                if failures >= MONITOR_FAILURES_BEFORE_UNAVAILABLE:
                    self.app.state.compute_ready = False
                    self.app.state.compute_state = "unavailable"
                    self._log(("unavailable", diagnosis),
                              f"[Compute] 计算连接未就绪（{diagnosis}）；保留本地记录与既有引擎")
                else:
                    self._log(("probe_failed", failures, diagnosis),
                              f"[Compute] 计算连接探活失败 "
                              f"{failures}/{MONITOR_FAILURES_BEFORE_UNAVAILABLE}"
                              f"（{diagnosis}）；仍按就绪对外服务")
            else:
                if self.app.state.compute_state == "unavailable":
                    self._log(("ready",), "[Compute] 计算连接已恢复")
                failures = 0
                self.app.state.compute_ready = True
                self.app.state.compute_state = "ready"

    async def stop(self):
        # Clear both flags before cleanup: half-released dependencies would make
        # non-execution routes raise 500 instead of the honest 503 (the gate reads
        # compute_installed to decide whether local control is still served).
        self.app.state.compute_ready = False
        self.app.state.compute_installed = False
        self.app.state.compute_state = "stopping"
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await finish_task(self.task)
        # finish_task deliberately lets a shielded install complete, which would
        # have republished readiness after the clearing above.
        self.app.state.compute_ready = False
        self.app.state.compute_installed = False
        if self.resources is not None:
            resources, self.resources = self.resources, None
            await finish_task(asyncio.create_task(self.cleanup(resources)))
        self.app.state.compute_state = "stopped"
