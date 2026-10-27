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
# Readiness split (/health, PROTOCOL §2.10): compute_state is the whole assembly; these
# say which dependency it is waiting for. Values are the HTTP contract with the app.
COMPUTE_NODE_STATES = ("local", "probing", "ready", "unreachable", "unauthorized")
RAG_STATES = ("probing", "ready", "unreachable", "unauthorized", "skipped")
# The launcher starts the local RAG data plane alongside the backend, so the first
# assembly attempts usually find it still loading. While only that LOCAL dependency is
# missing, retry at the initial interval instead of backing off (backoff exists to
# spare a remote node; it turned a ~14 s RAG cold start into a ~30 s wait). Past this
# much local waiting RAG is broken rather than starting, and the normal backoff applies.
LOCAL_WAIT_SECONDS = 120


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


class LocalDependencyUnavailable(Exception):
    """The compute node passed its probe; only a local dependency (RAG data plane) failed.

    Raised `from` the original error: diagnosis and guidance are taken from __cause__,
    so the log line is the same one the original failure would have printed.
    """


def compute_node_failure_state(exc) -> str:
    """Contract H: 401/403 from the authenticated /metrics read = unauthorized, else unreachable."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        # Only an HTTPStatusError carries a response; its request is always set.
        url = getattr(getattr(exc, "request", None), "url", None)
        if str(getattr(url, "path", "") or "").endswith("/metrics"):
            return "unauthorized"
    return "unreachable"


def rag_probe_host(cfg) -> str:
    """The host probe_rag_data actually dials (empty/wildcard binds are probed on loopback)."""
    host = str(cfg.get("host") or "127.0.0.1")
    return "127.0.0.1" if host in ("0.0.0.0", "::") else host


def rag_failure_state(exc) -> str:
    """rag_state for a failed probe: 401/403 = unauthorized (wrong key), else unreachable."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return "unauthorized" if status in (401, 403) else "unreachable"


def rag_failure_is_transient(exc) -> bool:
    """Connection-level failure or 5xx = the data plane is not up yet; anything else is not."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.TransportError, OSError))


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
    host = rag_probe_host(cfg)
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
                 retry_initial=2, retry_max=30, monitor_interval=5, clock=time.monotonic,
                 local_wait_max=LOCAL_WAIT_SECONDS):
        self.app = app
        self.assemble = assemble
        self.publish = publish
        self.cleanup = cleanup
        self.probe = probe
        self.retry_initial = max(0.01, retry_initial)
        self.retry_max = max(self.retry_initial, retry_max)
        self.monitor_interval = max(0.01, monitor_interval)
        self.local_wait_max = max(0.0, local_wait_max)
        self.resources = None
        self.task = None
        self.clock = clock
        self._log_key = None
        self._log_at = 0.0
        self._log_suppressed = 0
        self.app.state.compute_ready = False
        self.app.state.compute_state = "starting"
        self.app.state.compute_installed = False
        # Contract H. engine_ready mirrors "an engine is installed", not compute health.
        self.app.state.engine_ready = False
        self.app.state.compute_node_state = "probing" if probe is not None else "local"
        # Without a probe (in-process layout) nothing here ever probes RAG.
        self.app.state.rag_state = "probing" if probe is not None else "skipped"

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
        self.app.state.engine_ready = True
        self.app.state.compute_ready = True
        self.app.state.compute_state = "ready"

    def start(self):
        self.task = asyncio.create_task(self._run(), name="compute-startup")
        return self.task

    @staticmethod
    def _diagnose(exc) -> str:
        if isinstance(exc, LocalDependencyUnavailable) and exc.__cause__ is not None:
            exc = exc.__cause__
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
        local_since = None
        while self.resources is None:
            try:
                await self._install()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.app.state.compute_ready = False
                self.app.state.compute_state = "retrying"
                # Only-local failures (compute node passed) inside the local window
                # wait the fixed initial interval and leave the backoff untouched.
                local = False
                if isinstance(exc, LocalDependencyUnavailable):
                    now = self.clock()
                    if local_since is None:
                        local_since = now
                    local = now - local_since < self.local_wait_max
                wait = self.retry_initial if local else delay
                diagnosis = self._diagnose(exc)
                # The retry mode is part of the state: the fixed-interval → backoff switch
                # prints a line (the first backoff wait equals the fixed one, so keying on
                # the wait value would hide it, and would print every backoff step).
                self._log(("retrying", diagnosis, local),
                          f"[Compute] 计算装配未就绪（{diagnosis}），{wait:g}s 后重试"
                          + ("（本机依赖启动中，固定间隔）" if local else ""))
                await asyncio.sleep(wait)
                if not local:
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
                    # Same threshold as compute_state: the two never disagree in steady
                    # state. The monitor probe is the compute-node probe (RAG failures
                    # after install do not raise).
                    self.app.state.compute_node_state = compute_node_failure_state(exc)
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
                self.app.state.compute_node_state = "ready"

    async def stop(self):
        # Clear both flags before cleanup: half-released dependencies would make
        # non-execution routes raise 500 instead of the honest 503 (the gate reads
        # compute_installed to decide whether local control is still served).
        self.app.state.compute_ready = False
        self.app.state.compute_installed = False
        self.app.state.engine_ready = False
        self.app.state.compute_state = "stopping"
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await finish_task(self.task)
        # finish_task deliberately lets a shielded install complete, which would
        # have republished readiness after the clearing above.
        self.app.state.compute_ready = False
        self.app.state.compute_installed = False
        self.app.state.engine_ready = False
        if self.resources is not None:
            resources, self.resources = self.resources, None
            await finish_task(asyncio.create_task(self.cleanup(resources)))
        self.app.state.compute_state = "stopped"
