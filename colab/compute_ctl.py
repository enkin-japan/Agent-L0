#!/usr/bin/env python3
"""Colab 计算端租约：创建、监督、切换与回收。

入口是 ./colab/deploy.sh。凭据不进命令行参数、输出或源码包；VM 上执行的代码
全部来自 colab/vm_tasks/（经 remote_task 上传）与随包上传的 compute_vm.py。
"""
import argparse
import base64
from collections import deque
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent


def _load_sibling(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = _load_sibling('compute_common')

ROOT = HERE.parent
STATE = ROOT / '.colab-compute'
TASKS = HERE / 'vm_tasks'
KEY = ROOT / '.keys/tailnet-colab.authkey'
MODEL = 'RedHatAI/Qwen3.8-27B-INT4'
HOSTNAME = 'agent-compute'
VM_FILES = ('compute_vm.py', 'compute_fetch.py', 'compute_common.py')
# release_endpoint() reaches into this CLI's private client; only versions whose
# internals were checked may use it. Anything else fails closed (release-unconfirmed).
VERIFIED_COLAB_CLI = {'0.7.5'}
ECHO = True  # internal worker commands log to deploy.log only (their stdout is worker.log)

# ---- Lease states ----------------------------------------------------------
PREPARING, ALLOCATING, BOOTING, READY = 'preparing', 'allocating', 'booting', 'ready'
SWITCHING, SWITCH_FAILED = 'switching', 'switch-failed'
STOPPING, STOPPED, RELEASE_UNCONFIRMED = 'stopping', 'stopped', 'release-unconfirmed'
STOP_STATES = {STOPPING, STOPPED, RELEASE_UNCONFIRMED}
RETAINABLE_STATES = {READY, SWITCHING, SWITCH_FAILED}
SWITCH_FIELDS = ('switch', 'model', 'options', 'progress')

PHASE_LABELS = {
    'extracting': '解压代码', 'installing-python': '安装 Python 环境',
    'installing-dependencies': '安装 CUDA/推理依赖', 'checking-cuda-runtime': '验证 CUDA 依赖',
    'joining-tailnet': '加入 Tailnet', 'configuring-model': '配置所选模型',
    'downloading-model': '下载模型权重', 'loading-models': '加载模型及编译 CUDA Graph',
    'checking-authentication': '验证服务及鉴权', 'ready': 'VM 内服务已就绪', 'failed': '部署失败',
    'preparing-switch': '准备模型切换', 'stopping-inference': '停止现有推理进程',
    'checking-gpu-vacancy': '确认 GPU 已释放', 'updating-runtime': '更新推理运行时代码',
    'packing-source': '打包源码', 'uploading-source': '上传源码', 'starting-switch': '启动切换',
}

# ---- Options: one table of defaults, one table of limits -------------------
DEFAULTS = {
    'minutes': 120, 'auth_key': KEY, 'hostname': HOSTNAME, 'model': MODEL,
    'quantization': 'none', 'speculative': None, 'draft_tokens': None,  # None: per model
    'attention_backend': 'auto', 'kv_cache_dtype': 'auto', 'max_model_len': 65536,
    'gpu_memory': .72, 'stall_timeout': 600, 'bootstrap_timeout': 3600,
    'poll_interval': 10, 'health_interval': 60, 'lines': 80,
}
LIMITS = {
    'minutes': (1, 240), 'draft_tokens': (1, 5), 'max_model_len': (512, 131072),
    'gpu_memory': (.1, .85), 'stall_timeout': (30, 3600), 'bootstrap_timeout': (60, 14400),
    'poll_interval': (2, 60), 'health_interval': (30, 600), 'lines': (1, 10000),
}
MTP_DRAFT_TOKENS = 3
ATTENTION_BACKENDS = {'auto': 'auto', 'triton': 'TRITON_ATTN', 'flashinfer': 'FLASHINFER'}
KV_CACHE_DTYPES = ('auto', 'bfloat16', 'fp8')


def opt(args, name):
    value = getattr(args, name, None)
    return DEFAULTS[name] if value is None else value


def check_limit(name, value):
    low, high = LIMITS[name]
    if not low <= value <= high:
        raise ValueError(f'--{name.replace("_", "-")} 必须在 {low}–{high} 之间')
    return value


class LeaseStopped(RuntimeError):
    """A concurrent stop (or another lease) has taken ownership of the state."""


# ---- Output ----------------------------------------------------------------

def event(message, **fields):
    private_dir(STATE)
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + message
    if fields:
        line += ' ' + json.dumps(fields, ensure_ascii=False)
    with (STATE / 'deploy.log').open('a') as log:
        log.write(line + '\n')
    if ECHO:
        try:
            print(line, flush=True)
        except OSError:
            pass  # A closed log viewer must not abort resource cleanup.


# ---- Signals ---------------------------------------------------------------
INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


@contextmanager
def signal_handlers(handler):
    previous = {s: signal.getsignal(s) for s in INTERRUPT_SIGNALS}
    try:
        for s in previous:
            signal.signal(s, handler)
        yield
    finally:
        for s, old in previous.items():
            signal.signal(s, old)


def _raise_interrupt(*_):
    raise KeyboardInterrupt


def interrupts_raise():
    """SIGINT/SIGTERM/SIGHUP become KeyboardInterrupt inside; handlers restored after."""
    return signal_handlers(_raise_interrupt)


def cleanup_signals():
    # Ctrl+C must request cleanup once, not interrupt the cleanup subprocesses.
    return signal_handlers(signal.SIG_IGN)


@contextmanager
def allocation_signals():
    # Finish the bounded allocation transaction before honoring cancellation:
    # killing a local HTTP client cannot retract a server-side allocation.
    pending = False

    def requested(*_):
        nonlocal pending
        if not pending:
            event('收到停止请求：等待当前 VM 分配请求返回，再立即回收（请求上限 120 秒）')
        pending = True

    with signal_handlers(requested):
        yield
    if pending:
        raise KeyboardInterrupt


# ---- Local files and state -------------------------------------------------

def private_dir(path):
    if path.is_symlink():
        raise ValueError('私有目录不能是符号链接')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def read_private(path):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise ValueError(f'凭据文件 {path.name} 必须是本用户所有、仅本人可读写（600）的普通文件')
    return path.read_text().strip()


def env_credentials(paths):
    values = {}
    for path in paths:
        if path.exists():
            values.update(common.parse_env(read_private(path), names=common.TRANSFER_SECRETS))
    for name in common.REQUIRED_SECRETS:
        if len(values.get(name, '')) < common.MIN_SECRET_LENGTH:
            raise ValueError(f'{name} 缺失或短于 {common.MIN_SECRET_LENGTH} 个字符')
    return values


def current():
    path = STATE / 'current.json'
    return json.loads(path.read_text()) if path.exists() else None


def snapshot():
    """current() for cleanup paths: an unreadable state file reads as unknown, never raises."""
    try:
        return current()
    except (OSError, ValueError):
        return None


def save_state(data, *, new_lease=False):
    private_dir(STATE)
    # Separate from the foreground lease lock: stop must be able to acquire it.
    with (STATE / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            previous = current()
        except ValueError:
            # A corrupt record proves nothing about ownership; only the release
            # path may replace it, so a damaged file can never block a release.
            if data.get('state') not in STOP_STATES:
                raise LeaseStopped('状态文件已损坏；只允许回收路径覆盖它') from None
            previous = None
        if previous and previous.get('session') != data.get('session'):
            if not new_lease or previous.get('state') != STOPPED:
                raise LeaseStopped('状态文件属于另一个租约')
        elif previous:
            if previous.get('state') in STOP_STATES and data.get('state') not in STOP_STATES:
                raise LeaseStopped('租约已被并发停止')
            if previous.get('tailnet') and not data.get('tailnet'):
                data['tailnet'] = previous['tailnet']
            # The long-lived start/watch owner holds an older lease object.
            # A switch owns its options/progress; supervision must not roll it back.
            if previous.get('switch') != data.get('switch') and data.get('state') not in STOP_STATES:
                for key in (*SWITCH_FIELDS, 'state'):
                    if key in previous:
                        data[key] = previous[key]
            if previous.get('retain'):
                data['retain'] = True
        _write_state(data)


def _write_state(data):
    temporary = STATE / 'current.tmp'
    temporary.write_text(json.dumps(data, indent=2))
    temporary.chmod(0o600)
    temporary.replace(STATE / 'current.json')


def switch_update(session, job, **updates):
    """Atomic job ownership check: late switch callbacks never revive a stopped VM."""
    private_dir(STATE)
    with (STATE / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = current()
        if (not data or data.get('session') != session or data.get('state') in STOP_STATES
                or (data.get('switch') or {}).get('job') != job):
            raise LeaseStopped('切换任务已不再持有该租约')
        data.update(updates)
        _write_state(data)
        return data


def prior_resources(previous):
    previous = previous or {}
    return {**previous.get('predecessor', {}),
            **{k: previous[k] for k in ('endpoint', 'tailnet') if k in previous}}


def registered_models():
    """CUDA registry entries. Every one must be a prequantized checkpoint (D25, 2026-10-31):
    the A100 is bandwidth-bound, so online fp8 quantization would halve decode speed and
    no other precision is admitted. An entry without the flag is a config error, not a
    deployable model."""
    import yaml
    registry = yaml.safe_load((ROOT / 'config.yaml').read_text())['inference']['model_registry']
    cuda = {e['repo']: e for e in registry if 'mlx' not in e['repo'].lower() and e.get('revision')}
    for repo, entry in cuda.items():
        if (entry.get('cuda_deployment') or {}).get('prequantized') is not True:
            raise ValueError(f'CUDA 注册条目一律预量化（D25）：{repo} 缺 cuda_deployment.prequantized: true')
    return cuda


def deployment_options(args):
    model = opt(args, 'model')
    entry = registered_models().get(model)
    if not entry:
        raise ValueError('模型必须是已注册的 CUDA 模型；运行 deploy.sh models 查看')
    quantization = opt(args, 'quantization')
    if quantization != 'none':  # D25: registry entries are prequantized; no online quantization
        raise ValueError('CUDA 条目一律预量化（D25），不接受 --quantization；旧 fp8 路径见 git 历史')
    supports_mtp = (entry.get('cuda_deployment') or {}).get('supports_mtp') is True
    speculative = opt(args, 'speculative')
    if speculative is None:  # MTP wherever the registry says the model has it
        speculative = 'mtp' if supports_mtp else 'off'
    draft_tokens = getattr(args, 'draft_tokens', None)
    if speculative not in {'off', 'mtp'}:
        raise ValueError('--speculative 只能是 off 或 mtp')
    if speculative == 'off' and draft_tokens is not None:
        raise ValueError('--draft-tokens 只在 MTP 开启时有效（该模型默认不开，需 --speculative mtp）')
    if speculative == 'mtp':
        draft_tokens = check_limit('draft_tokens',
                                   MTP_DRAFT_TOKENS if draft_tokens is None else draft_tokens)
        if not supports_mtp:
            raise ValueError('该注册模型不支持 MTP')
    attention, kv_cache = opt(args, 'attention_backend'), opt(args, 'kv_cache_dtype')
    if attention not in ATTENTION_BACKENDS or kv_cache not in KV_CACHE_DTYPES:
        raise ValueError('无效的 attention backend 或 KV cache dtype')
    if speculative == 'mtp' and entry.get('dialect') == 'qwen35':
        attention = 'triton' if attention == 'auto' else attention
        kv_cache = 'bfloat16' if kv_cache == 'auto' else kv_cache
    return {'model': model, 'revision': entry['revision'], 'quantization': quantization,
            'speculative': speculative, 'draft_tokens': draft_tokens,
            'speculative_config': ({'method': 'mtp', 'num_speculative_tokens': draft_tokens}
                                   if speculative == 'mtp' else None),
            'attention_backend': ATTENTION_BACKENDS[attention], 'kv_cache_dtype': kv_cache,
            'max_model_len': opt(args, 'max_model_len'),
            'gpu_memory_utilization': opt(args, 'gpu_memory'),
            'stall_timeout': opt(args, 'stall_timeout'),
            'bootstrap_timeout': opt(args, 'bootstrap_timeout')}


def reserve_lease(args):
    previous = current()
    if previous and previous.get('state') != STOPPED:
        raise ValueError('上一个租约尚未确认停止；先运行 status / stop')
    options = deployment_options(args)
    now = time.time()
    lease = {'session': common.SESSION_PREFIX + uuid.uuid4().hex[:12], 'state': PREPARING,
             'started': now, 'deadline': now + opt(args, 'minutes') * 60,
             'gpu': common.GPU_NAME, 'model': options['model'], 'hostname': opt(args, 'hostname'),
             'auth_key': str(opt(args, 'auth_key')), 'ephemeral': True, 'options': options,
             'allocation_attempted': False, 'retain': bool(getattr(args, 'retain', False)),
             'vm_layout': dict(common.LAYOUT),
             'supervision_options': {'poll_interval': opt(args, 'poll_interval'),
                                     'health_interval': opt(args, 'health_interval')},
             'predecessor': prior_resources(previous)}
    save_state(lease, new_lease=True)
    return lease


def supervision_option(lease, name):
    return (lease.get('supervision_options') or {}).get(name, DEFAULTS[name])


# ---- Source archive --------------------------------------------------------
REQUIRED_SOURCES = {'config.yaml', 'devctl.sh', 'backend/scripts/fetch_model.py',
                    'inference/server.py', 'rag/compute_server.py'}


def source_allowed(name):
    p = Path(name)
    if p.is_absolute() or '..' in p.parts or any(x.startswith('.') for x in p.parts):
        return False
    if name in {'config.yaml', 'devctl.sh', 'scripts/lib_edge.sh', 'backend/scripts/fetch_model.py'}:
        return True
    return p.suffix == '.py' and (
        name.startswith('backend/framework/') or name.startswith('inference/')
        or p.parent == Path('rag'))


def make_archive(root, destination):
    listed = subprocess.check_output(['git', '-C', str(root), 'ls-files', '--cached', '--others',
                                      '--exclude-standard', '-z']).decode().split('\0')
    names = sorted(n for n in listed if n and source_allowed(n))
    if not REQUIRED_SOURCES.issubset(names):
        raise ValueError('缺少必需的计算端源码')
    with tarfile.open(destination, 'w:gz') as tar:
        for name in names:
            path = root / name
            linked = any(p.is_symlink() for p in path.parents if p != root and root in p.parents)
            if (path.is_symlink() or linked or not path.is_file()
                    or not path.resolve().is_relative_to(root.resolve())):
                raise ValueError('源码打包拒绝链接和非普通文件')
            tar.add(path, arcname=name, recursive=False)
    destination.chmod(0o600)
    return len(names)


# ---- Colab CLI and VM tasks ------------------------------------------------

def cli(*args, timeout=90, capture=False):
    # Colab itself owns its auth/session store. Never read, copy, or restore it.
    return subprocess.run(['colab', *args], check=True, timeout=timeout,
                          text=True, capture_output=capture, start_new_session=True)


def remote(session, code, timeout=40):
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py') as f:
        f.write(code)
        f.flush()
        return cli('exec', '-s', session, '-f', f.name, '--timeout', str(timeout),
                   timeout=timeout + 20, capture=True).stdout


def task_code(name, params):
    return (TASKS / f'{name}.py').read_text() + f'\nmain(json.loads({json.dumps(params)!r}))\n'


def remote_task(session, name, params, timeout=40):
    return remote(session, task_code(name, params), timeout=timeout)


def marker_line(output, marker):
    match = re.search(rf'^{marker} (.*)$', output, re.M)
    return match[1] if match else None


def colab_cli_interpreter():
    executable = Path(subprocess.check_output(['which', 'colab'], text=True).strip()).resolve()
    interpreter = executable.read_text().splitlines()[0].removeprefix('#!')
    if not Path(interpreter).is_file():
        raise RuntimeError('找不到 Colab CLI 的 Python 解释器')
    return interpreter


def colab_cli_version():
    try:
        return subprocess.run(
            [colab_cli_interpreter(), '-c',
             "import importlib.metadata as m; print(m.version('google-colab-cli'))"],
            check=True, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, RuntimeError, UnicodeDecodeError, subprocess.SubprocessError):
        return 'unknown'


def server_sessions():
    out = re.sub(r'\x1b\[[0-9;]*m', '', cli('sessions', capture=True).stdout)
    pairs = re.findall(r'^\[([^\]]+)\]\s+(\S+)\s+\| Hardware:', out, re.M)
    if not pairs and 'No active sessions found on server.' not in out:
        raise RuntimeError(f'无法解析 colab sessions 输出（CLI {colab_cli_version()}，'
                           f'已验证版本 {sorted(VERIFIED_COLAB_CLI)}），无法核对服务端分配')
    return {endpoint: name for name, endpoint in pairs}


def release_endpoint(endpoint):
    # CLI 0.7.5 stop silently succeeds after exec prunes a local record. Use the
    # same installed CLI client/auth provider to unassign the recorded endpoint;
    # it consumes its normal auth internally. Never inspect or rewrite auth files.
    version = colab_cli_version()
    if version not in VERIFIED_COLAB_CLI:
        raise RuntimeError(f'Colab CLI {version} 的私有释放接口未经验证；'
                           '请在 Colab 控制台手动释放该 VM 后再运行 stop 核对')
    code = 'from colab_cli.common import state; import sys; state.client.unassign(sys.argv[1])'
    subprocess.run([colab_cli_interpreter(), '-c', code, endpoint], check=True, timeout=60,
                   capture_output=True, start_new_session=True)


# ---- Tailnet and resource inventory ----------------------------------------

def tailnet_status():
    result = subprocess.run(['tailscale', 'status', '--json'], check=True,
                            capture_output=True, text=True, timeout=20)
    data = json.loads(result.stdout)
    if data.get('BackendState') != 'Running':
        raise RuntimeError('本机 Tailscale 未运行，无法核对计算端节点名')
    return data


def inspect_resources(hostname):
    """Read-only inventory. Peer visibility is limited by tailnet access policy."""
    assignments = server_sessions()
    network = tailnet_status()
    tailnet = network.get('CurrentTailnet') or {}
    peers = [network.get('Self') or {}, *(network.get('Peer') or {}).values()]
    recorded = current() or {}
    recorded_id = (recorded.get('tailnet') or recorded.get('predecessor', {}).get('tailnet')
                   or {}).get('id')
    prefixes = (hostname + '-', common.SESSION_PREFIX, common.LEGACY_SESSION_PREFIX)
    conflicts = []
    for peer in peers:
        name = peer.get('DNSName', '').rstrip('.').split('.')[0]
        host = peer.get('HostName', '')
        if ((recorded_id and peer.get('ID') == recorded_id) or hostname in (name, host)
                or name.startswith(prefixes) or host.startswith(prefixes[1:])):
            conflicts.append({k: peer.get(k) for k in ('ID', 'DNSName', 'HostName', 'Online')})
    return {'sessions': assignments, 'magic_dns': tailnet.get('MagicDNSEnabled'),
            'dns_suffix': tailnet.get('MagicDNSSuffix'), 'visible_compute_nodes': conflicts}


def check_start_resources(hostname, previous):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]', hostname):
        raise ValueError('hostname 必须是小写 DNS 标签（1–63 个字符）')
    inventory = inspect_resources(hostname)
    assignments = inventory['sessions']
    if previous and previous.get('endpoint') in assignments:
        raise RuntimeError('记录中的 VM 仍处于分配状态（still allocated）；先运行 stop')
    if any(common.is_owned_session(name) for name in assignments.values()):
        raise RuntimeError('仍有本工具创建的 VM 处于分配状态；先用 status 核对并回收')
    if '?' in assignments.values():
        raise RuntimeError('服务端有身份未知的分配，需人工确认后再启动')
    if inventory['magic_dns'] is not True or not inventory['dns_suffix']:
        raise RuntimeError('未确认 MagicDNS 已开启；先在 Tailnet DNS 控制台开启')
    if inventory['visible_compute_nodes']:
        raise RuntimeError('计算端节点名或遗留节点仍可见；用 status 核对，只删除确认失效的设备')
    return inventory['dns_suffix'].rstrip('.')


def tailnet_address(host):
    """IPv4 the tailnet itself maps `host` to: the source MagicDNS answers from."""
    for peer in (tailnet_status().get('Peer') or {}).values():
        if peer.get('DNSName', '').rstrip('.') == host:
            return next((ip for ip in peer.get('TailscaleIPs') or () if '.' in ip), None)
    return None


def system_addresses(host):
    # Use the OS resolver (including macOS split DNS), not host/nslookup, with a
    # bounded subprocess so a resolver stall cannot hang the launcher.
    code = ('import socket,json,sys; print(json.dumps(sorted({r[4][0] for r in '
            'socket.getaddrinfo(sys.argv[1],None,socket.AF_INET,socket.SOCK_STREAM)})))')
    try:
        result = subprocess.run([sys.executable, '-c', code, host],
                                check=True, capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def verify_dns(host, ip):
    """Fail only when the tailnet does not map the fixed name to the new VM.

    This Mac's resolver is checked as well, but a mismatch there is a local cache:
    while the name is absent between VMs, every client lookup gets "no such host"
    and mDNSResponder can keep that answer far longer than the gap (observed: still
    stale 20+ minutes after the new node registered). Releasing a ready VM for it
    would only repeat the gap, so it is reported with the remedy instead.

    Likewise an unreadable local tailscale (daemon restarting, Mac waking) proves
    nothing about the VM; the VM itself already refused any suffixed name when it
    recorded its identity. Returns 'ok', 'stale' or 'unknown'.
    """
    seen, error = False, None
    for attempt in range(3):  # brief propagation after registration
        try:
            address = tailnet_address(host)
            seen = True
            if address == ip:
                break
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            error = type(exc).__name__
        if attempt < 2:
            time.sleep(5)
    else:
        if seen:
            raise RuntimeError('Tailnet 中的计算端固定域名未指向新 VM；用 status 核对同名节点')
        event('读不到本机 Tailscale 状态，未能核对固定域名；VM 保持就绪', host=host, ip=ip,
              error_type=error)
        return 'unknown'
    for attempt in range(3):
        if system_addresses(host) == [ip]:
            return 'ok'
        if attempt < 2:
            time.sleep(5)
    event('本机系统 DNS 仍缓存旧结果；Tailnet 已指向新 VM，VM 保持就绪',
          host=host, ip=ip, refresh='sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder')
    return 'stale'


# ---- Release ---------------------------------------------------------------

def logout_tailnet(data):
    """A logout failure must never prevent release of a billable VM."""
    data['tailnet_cleanup'] = 'logout-unconfirmed'
    layout = common.layout_of(data)
    try:
        out = remote_task(data['session'], 'logout', {'private': layout['private'],
                                                      'tailscale': layout['tailscale']}, 15)
        if re.search(r'^COMPUTE_LOGOUT_OK$', out, re.M):
            data['tailnet_cleanup'] = ('ephemeral-logout-confirmed' if data.get('ephemeral')
                                       else 'logout-confirmed-removal-unverified')
    except Exception:
        pass


def stop_owned(data, operator_confirmed=False):
    with cleanup_signals():
        for attempt in range(3):
            try:
                return stop_attempt(data, operator_confirmed)
            except LeaseStopped:
                raise
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                event('回收尚未确认', attempt=attempt + 1, error_type=type(exc).__name__)
                if attempt == 2:
                    event('回收失败：请运行 ./colab/deploy.sh stop 重试；不能视为已释放')
                    raise
                time.sleep(2)


def stop_attempt(data, operator_confirmed=False):
    name = data['session']
    if not common.is_owned_session(name):
        raise ValueError('拒绝停止非本工具创建的会话')
    private_dir(STATE)
    # A second stop (including the lease owner's finally) waits for the first,
    # then verifies its result or takes over after failure/process death.
    with (STATE / 'allocation.lock').open('a') as allocation_lock, \
            (STATE / 'cleanup.lock').open('a') as lock:
        event('等待正在进行的 VM 分配结束（如有），然后回收')
        fcntl.flock(allocation_lock, fcntl.LOCK_EX)
        fcntl.flock(lock, fcntl.LOCK_EX)
        latest = snapshot()  # unreadable: release from the caller's own lease view
        if latest:
            if latest.get('session') != name:
                raise LeaseStopped('回收状态属于另一个租约')
            if latest.get('state') == STOPPED and data.get('state') != STOPPED:
                return  # Another stop completed while this caller was waiting.
            data.update(latest)
        try:
            _stop_owned(data, operator_confirmed)
        except BaseException:
            data['state'] = RELEASE_UNCONFIRMED
            try:
                save_state(data)
            except (OSError, LeaseStopped):
                pass  # Preserve the original error; never overwrite a new lease.
            raise


def _confirm_no_allocation(name, first):
    # A non-zero `colab new` exit is not proof that the server allocated nothing: the
    # CLI may have timed out after the server accepted, and a VM still provisioning
    # shows up late. Require the server list to stay free of this session and of
    # unattributed `?` VMs across three reads, 5 s apart.
    views = [first]
    for _ in range(2):
        time.sleep(5)
        views.append(server_sessions())
    return all(name not in v.values() and '?' not in v.values() for v in views)


def _stop_owned(data, operator_confirmed=False):
    name = data['session']
    event('正在核对并回收 VM', session=name)
    if data.get('allocation_attempted') is False and not data.get('endpoint'):
        data['state'] = STOPPED
        save_state(data)
        event('启动已取消，尚未申请 VM，无计费资源需要释放')
        return
    assignments = server_sessions()
    endpoint = data.get('endpoint') or next((e for e, n in assignments.items() if n == name), None)
    if not endpoint:
        proven = data.get('allocation_failed') or operator_confirmed
        if proven and _confirm_no_allocation(name, assignments):
            data['state'] = STOPPED
            save_state(data)
            event('申请未成功且服务端连续三次核对均无本会话或身份不明的 VM；无计费资源需要释放',
                  session=name, allocation_failed=data.get('allocation_failed'),
                  operator_confirmed=bool(operator_confirmed))
            return
        data['state'] = RELEASE_UNCONFIRMED
        save_state(data)
        raise RuntimeError('分配身份未知；先人工核对 colab sessions；未声称已释放。'
                           '确认服务端没有本工具的 VM 后，用 stop --confirm-unallocated 清除本地租约')
    data['endpoint'] = endpoint
    data['state'] = STOPPING
    save_state(data)
    if endpoint in assignments and assignments[endpoint] not in {name, '?'}:
        raise RuntimeError('记录的端点已归属另一个命名会话；拒绝释放')
    # A name that now maps to another endpoint must not be used for remote logout
    # or CLI stop. The recorded endpoint remains the only release authority.
    named_endpoint = next((e for e, n in assignments.items() if n == name), None)
    if named_endpoint == endpoint:
        event('正在注销 Tailscale 临时节点')
        logout_tailnet(data)
    else:
        data.setdefault('tailnet_cleanup', 'logout-unconfirmed')
    if endpoint in assignments:
        if named_endpoint == endpoint:
            event('正在请求 Colab 释放 VM')
            try:
                cli('stop', '-s', name, timeout=90)
            except subprocess.SubprocessError:
                pass
        if endpoint in server_sessions():
            event('会话停止未生效，按已记录端点重试释放')
            release_endpoint(endpoint)
        for _ in range(6):
            if endpoint not in server_sessions():
                break
            time.sleep(3)
        else:
            data['state'] = RELEASE_UNCONFIRMED
            save_state(data)
            raise RuntimeError('服务端仍显示该 VM；释放未确认')
    data['state'] = STOPPED
    save_state(data)
    event('已确认 VM 从 Colab 服务端移除', session=name, tailnet_cleanup=data['tailnet_cleanup'])


# ---- Progress following (shared by start and switch) -----------------------

def follow_progress(fetch, handle, *, deadline, stall_timeout, poll_interval,
                    log_prefix, log_fields=None):
    """Poll fetch() until handle(progress, idle_s) returns non-None.

    Bounded twice: by the wall-clock deadline and by stall_timeout without a new
    (phase, activity_seq) signature. New VM log lines are relayed once each.
    """
    last, last_activity, seen = None, time.monotonic(), 0
    while time.time() < deadline:
        progress = fetch()
        signature = (progress['phase'], progress.get('activity_seq', 0))
        if signature != last:
            last, last_activity = signature, time.monotonic()
        for seq, line in progress.get('log_tail', []):
            if seq > seen:
                event(log_prefix + line, **(log_fields or {}))
                seen = seq
        result = handle(progress, round(time.monotonic() - last_activity))
        if result is not None:
            return result
        if time.monotonic() - last_activity > stall_timeout:
            raise RuntimeError('部署停滞：长时间没有可观测进度')
        time.sleep(poll_interval)
    raise RuntimeError('部署超时')


def poll_progress(lease, deadline):
    params = {'private': common.layout_of(lease)['private'], 'marker': 'COMPUTE_PROGRESS'}
    for attempt in range(3):
        remaining = int(deadline - time.time())
        if remaining <= 0:
            raise RuntimeError('查询进度时已到部署期限')
        try:
            out = remote_task(lease['session'], 'progress', params, timeout=min(120, remaining))
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            if attempt == 2:
                raise
            event('进度查询重试', attempt=attempt + 1, error_type=type(exc).__name__)
            time.sleep(min(5, max(0, deadline - time.time())))
            continue
        line = marker_line(out, 'COMPUTE_PROGRESS')
        if line is None:
            raise RuntimeError('缺少结构化进度输出')
        return json.loads(line)


def download_display(download):
    return (f"{download['bytes_downloaded'] / 2**30:.2f}/{download['bytes_total'] / 2**30:.2f}"
            f" GiB ({download['percent']}%, {download['bytes_per_second'] / 2**20:.1f} MiB/s)")


def collect_failure_diagnostic(lease):
    layout = common.layout_of(lease)
    out = remote_task(lease['session'], 'sanitized_text',
                      {'mode': 'diagnostic', 'private': layout['private'],
                       'app': layout['app'], 'helper': layout['helper']}, timeout=120)
    text = marker_line(out, 'COMPUTE_TEXT')
    if text is None:
        raise RuntimeError('无法取得 VM 脱敏诊断')
    target = STATE / f'failure-{lease["session"]}.log'
    target.write_bytes(base64.b64decode(text, validate=True))
    target.chmod(0o600)
    event('已保存 VM 脱敏诊断', path=str(target))


# ---- Start -----------------------------------------------------------------

def lease_alive(lease):
    saved = current()
    if not saved or saved.get('session') != lease['session'] or saved.get('state') in STOP_STATES:
        raise LeaseStopped('租约已不再处于活动状态')


def start(args):
    job = getattr(args, 'job', None)
    lease = current() if job else reserve_lease(args)
    if not lease or (job and lease.get('session') != job):
        raise LeaseStopped('后台任务身份已变化')
    if lease.get('state') != PREPARING:
        event('启动已取消')
        return
    phase = {'allocated': False, 'bootstrapped': False}
    # The handlers stay installed through finish_start; stop_owned then ignores
    # further signals itself so a repeated Ctrl+C cannot interrupt the release.
    with interrupts_raise():
        try:
            _start_owned(lease, phase)
            monitor_ready(lease)
        except KeyboardInterrupt:
            event('监督已中断；VM 保留，请手动 stop'
                  if lease.get('retain') and lease.get('state') == READY
                  else '收到中断：开始回收；请等待服务端确认，勿强制杀进程')
            raise
        except LeaseStopped:
            event('租约已由另一个进程停止')
        except Exception as exc:
            # finish_start may raise its own error; record the original cause first.
            try:
                event('启动失败' + ('，开始回收' if phase['allocated'] else ''),
                      error_type=type(exc).__name__,
                      reason=str(exc) if isinstance(exc, (ValueError, RuntimeError)) else '')
            except OSError:
                pass  # Logging must never replace the original failure.
            if phase['bootstrapped']:
                try:
                    collect_failure_diagnostic(lease)
                except Exception as exc:
                    event('诊断收集失败', error_type=type(exc).__name__)
            raise
        finally:
            finish_start(lease, phase['allocated'])


def finish_start(lease, allocated):
    """Release whatever this start allocated, unless it is a retained READY VM."""
    if not allocated:
        # A local preparation failure cannot have allocated a billable VM.
        lease.update(state=STOPPED, allocation_attempted=False)
        save_state(lease)
        return
    saved = snapshot()
    if saved is not None and (saved.get('session') != lease['session']
                              or saved.get('state') == STOPPED):
        return
    # An unreadable state file must not skip release: fall back to this process's view.
    view = saved if saved is not None else lease
    if view.get('retain') and view.get('state') in RETAINABLE_STATES:
        mark_supervision_interrupted(lease['session'])
        event('VM 保留，需手动运行 ./colab/deploy.sh stop 回收')
    else:
        stop_owned(lease)


def _start_owned(lease, phase):
    lease['worker_pid'] = os.getpid()
    save_state(lease)
    credentials, suffix = _preflight(lease)
    lease['expected_dns'] = lease['hostname'] + '.' + suffix
    save_state(lease)
    event('预检查通过')
    with tempfile.TemporaryDirectory(prefix='agent-colab-compute-') as temp:
        temp = Path(temp)
        archive = temp / 'source.tar.gz'
        event('开始打包计算端源码')
        count = make_archive(ROOT, archive)
        event('源码打包完成', files=count,
              bytes=archive.stat().st_size if archive.exists() else 0)
        _allocate(lease, phase)
        _verify_gpu(lease)
        _upload_bundle(lease, temp, archive, credentials)
        _kick_bootstrap(lease)
        phase['bootstrapped'] = True
    credentials.clear()
    lease['state'] = BOOTING
    save_state(lease)
    _await_ready(lease)


def _preflight(lease):
    event('开始预检查：遗留 VM、Tailnet 节点、MagicDNS 和本机配置')
    suffix = check_start_resources(lease['hostname'], lease.get('predecessor'))
    credentials = env_credentials([ROOT / '.env', ROOT / '.env.local'])
    key = read_private(Path(lease['auth_key']))
    if not key.startswith('tskey-auth-') or any(c.isspace() for c in key):
        raise ValueError('Tailnet key 文件必须只含一个 auth key')
    credentials['TS_AUTHKEY'] = key
    return credentials, suffix


def _allocate(lease, phase):
    with allocation_signals(), (STATE / 'allocation.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lease.update(state=ALLOCATING, allocation_attempted=True)
        save_state(lease)  # CAS rejects a stop issued during preflight before any paid call.
        phase['allocated'] = True
        event('开始申请 A100 40GB VM', session=lease['session'])
        try:
            cli('new', '-s', lease['session'], '--gpu', common.GPU_NAME, timeout=120)
        except subprocess.CalledProcessError as exc:
            # The CLI itself reported the request failed (no A100, quota, network).
            # allocation_attempted stays True: stop still has to see the server list
            # free of this session and of unattributed VMs before recording STOPPED.
            lease['allocation_failed'] = 'cli-error'
            save_state(lease)
            event('Colab CLI 申请 VM 失败', returncode=exc.returncode)
            raise
        lease['endpoint'] = next((e for e, n in server_sessions().items()
                                  if n == lease['session']), None)
        if not lease['endpoint']:
            raise RuntimeError('未能确认新分配的身份')
        save_state(lease)


def _verify_gpu(lease):
    event('VM 已分配，检查 GPU 型号及占用')
    out = remote_task(lease['session'], 'gpu_check',
                      {'private': common.layout_of(lease)['private'],
                       'gpu_name': common.GPU_NAME, 'memory_mib': common.GPU_MEMORY_MIB})
    if 'COMPUTE_GPU_VERIFIED' not in out:
        raise RuntimeError('GPU 校验未通过')
    event('GPU 型号和空闲检查通过')


def _upload_bundle(lease, temp, archive, credentials):
    private = common.layout_of(lease)['private']
    secret = temp / 'credentials.json'
    secret.write_text(json.dumps(credentials))
    secret.chmod(0o600)
    network = temp / 'network.json'
    network.write_text(json.dumps({'hostname': lease['hostname'],
                                   'expected_dns': lease['expected_dns'],
                                   'vm_layout': lease['vm_layout'], **lease['options']}))
    files = [(archive, 'source.tar.gz'), (secret, 'credentials.json'),
             (network, 'network.json'),
             *[(ROOT / 'colab' / name, name) for name in VM_FILES],
             (ROOT / 'colab/requirements-compute.lock', 'requirements.txt')]
    for local, name in files:
        event('开始上传', file=name)
        cli('upload', '-s', lease['session'], str(local), f'{private}/{name}',
            timeout=120, capture=True)
        event('上传完成', file=name)
    secret.unlink()
    event('代码、启动器和私密配置上传完成；开始远端部署')


def _kick_bootstrap(lease):
    layout = common.layout_of(lease)
    out = remote_task(lease['session'], 'bootstrap_kick',
                      {'private': layout['private'], 'helper': layout['helper']})
    if 'COMPUTE_BOOTSTRAP_STARTED' not in out:
        raise RuntimeError('未确认远端部署进程已启动')
    event('远端部署进程已启动')


def _await_ready(lease):
    options = lease['options']
    deadline = min(lease['deadline'], lease['started'] + options['bootstrap_timeout'])

    def fetch():
        lease_alive(lease)
        return poll_progress(lease, deadline)

    def handle(progress, idle_s):
        summary = {k: progress[k] for k in ('phase', 'detail', 'download') if k in progress}
        display = dict(summary)
        if progress.get('download'):
            display['download'] = download_display(progress['download'])
        event(PHASE_LABELS.get(progress['phase'], '部署进度'), **display,
              elapsed_s=round(time.time() - lease['started']), no_progress_s=idle_s)
        lease['progress'] = summary
        if progress.get('tailnet'):
            lease['tailnet'] = progress['tailnet']
        save_state(lease)
        if progress['phase'] == 'failed':
            raise RuntimeError('VM 部署失败；见已保存的脱敏诊断')
        if progress['phase'] == 'ready':
            local_dns = verify_dns(lease['expected_dns'], progress['host'])
            lease.update(state=READY, host=lease['expected_dns'], ip=progress['host'],
                         local_dns=local_dns)
            save_state(lease)
            event('READY：计算端已就绪', host=lease['host'], local_dns=local_dns,
                  stop='./colab/deploy.sh stop',
                  mode='手动保留：仅 ./colab/deploy.sh stop 回收' if lease.get('retain')
                  else '限时租约：到期或健康失败自动回收')
            return True
        if time.time() - lease['started'] > options['bootstrap_timeout']:
            raise RuntimeError('超出部署时限')
        return None

    follow_progress(fetch, handle, deadline=deadline, stall_timeout=options['stall_timeout'],
                    poll_interval=supervision_option(lease, 'poll_interval'), log_prefix='VM | ')


# ---- Supervision -----------------------------------------------------------

def check_compute(lease):
    params = {'ports': {'infer': common.INFER_PORT, 'rag': common.RAG_PORT}}
    for attempt in range(3):
        try:
            line = marker_line(remote_task(lease['session'], 'health', params, timeout=30),
                               'COMPUTE_HEALTH')
            if line and json.loads(line) == {'infer': True, 'rag': True}:
                event('服务健康检查通过', session=lease['session'])
                return
        except (OSError, subprocess.SubprocessError):
            pass
        event('服务健康未确认', attempt=attempt + 1)
        if attempt < 2:
            time.sleep(5)
    raise RuntimeError('计算端连续三次健康检查失败')


def lease_ended(saved, lease):
    return (not saved or saved.get('session') != lease['session']
            or saved.get('state') in STOP_STATES)


def monitor_ready(lease):
    """Read-only service supervision; retained leases never expire or auto-release."""
    lease['supervision'] = {'state': 'active', 'pid': os.getpid(), 'since': time.time(),
                            'switch_aware': True}
    save_state(lease)
    event('后台监督已确认运行', session=lease['session'], retain=lease.get('retain', False))
    interval = supervision_option(lease, 'health_interval')
    while True:
        saved = current()
        if lease_ended(saved, lease):
            return
        retained = bool(saved.get('retain'))
        if not retained and time.time() >= lease['deadline']:
            event('租期到期，开始回收')
            return
        if saved.get('state') not in {SWITCHING, SWITCH_FAILED}:
            try:
                check_compute(lease)
            except RuntimeError:
                # A switch may begin while a health request is in flight.
                latest = current() or {}
                if (latest.get('state') in {SWITCHING, SWITCH_FAILED}
                        or latest.get('switch') != saved.get('switch')):
                    continue
                if not retained:
                    raise
                event('服务健康未确认；按手动保留设置继续保留 VM，请检查日志或手动 stop')
        wake_at = time.time() + interval
        if not retained:
            wake_at = min(wake_at, lease['deadline'])
        while time.time() < wake_at:
            if lease_ended(current(), lease):
                return
            time.sleep(min(1, max(0, wake_at - time.time())))


def mark_supervision_interrupted(session):
    saved = snapshot()
    if saved and saved.get('session') == session and saved.get('state') in RETAINABLE_STATES:
        saved['supervision'] = {**saved.get('supervision', {}), 'state': 'interrupted',
                                'pid': os.getpid(), 'since': time.time()}
        try:
            save_state(saved)
        except LeaseStopped:
            pass  # A concurrent explicit stop always wins.


def supervisor_status(lease):
    """Read process identity without printing its arguments or trusting a recycled PID."""
    result = dict(lease or {})
    status = dict(result.get('supervision') or {})
    if status.get('state') != 'active':
        return result
    pid = status.get('pid')
    alive = None
    if isinstance(pid, int) and pid > 1:
        try:
            probe = subprocess.run(['ps', '-p', str(pid), '-o', 'uid=', '-o', 'command='],
                                   capture_output=True, text=True, timeout=5)
            if probe.returncode == 0:
                uid, _, command = probe.stdout.strip().partition(' ')
                words = command.strip().split()
                alive = (uid == str(os.getuid()) and len(words) > 2
                         and Path(words[1]).resolve() == Path(__file__).resolve()
                         and words[2] in {'_watch', '_worker'}
                         and '--job' in words
                         and words[words.index('--job') + 1] == result.get('session'))
            elif probe.returncode == 1 and not probe.stderr.strip():
                alive = False
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            pass
    status['state'] = 'active' if alive else 'interrupted' if alive is False else 'unknown'
    result['supervision'] = status
    return result


def spawn_worker(command, fd, log_mode):
    """Detach an internal worker that inherits exactly one lock fd."""
    with (STATE / 'worker.log').open(log_mode) as log:
        return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, close_fds=True, pass_fds=(fd,), cwd=ROOT)


def early_exit_code(child):
    """Report an immediate argument/preflight failure; a live worker means 0."""
    try:
        return child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        return 0


def worker_command(name, fd_flag, fd, job):
    return [sys.executable, str(Path(__file__).resolve()), name, fd_flag, str(fd), '--job', job]


def watch_existing(args, lease_fd, *, worker=False):
    """Resume supervision of an owned READY VM; never allocate or load a model."""
    lease = current()
    if not lease or lease.get('state') != READY or not lease.get('endpoint'):
        raise ValueError('watch 需要一个已有的 READY 租约')
    if worker and getattr(args, 'job', None) != lease['session']:
        raise LeaseStopped('watch 身份已变化')
    if server_sessions().get(lease['endpoint']) != lease['session']:
        raise LeaseStopped('未能确认 VM 身份；未改动任何 VM')
    if not worker:
        if getattr(args, 'retain', False):
            lease['retain'] = True
        lease['supervision_options'] = {**(lease.get('supervision_options') or {}),
                                        'health_interval': opt(args, 'health_interval')}
        lease['supervision'] = {'state': 'starting', 'since': time.time(), 'switch_aware': False}
    lease['worker_pid'] = os.getpid() if worker else None
    save_state(lease)
    if not worker:
        child = spawn_worker(worker_command('_watch', '--lease-fd', lease_fd, lease['session']),
                             lease_fd, 'a')
        event('已有 VM 的后台监督启动中（尚未确认）', pid=child.pid,
              retain=lease.get('retain', False), stop='./colab/deploy.sh stop')
        return early_exit_code(child)
    try:
        monitor_ready(lease)
    finally:
        saved = snapshot()
        view = saved if saved is not None else lease  # same fallback as finish_start
        if (view.get('session') == lease['session']
                and view.get('state') != STOPPED and not view.get('retain')):
            stop_owned(lease)
    return 0


def run_watch_worker(args, lease_fd):
    try:
        with interrupts_raise():
            return watch_existing(args, lease_fd, worker=True)
    finally:
        mark_supervision_interrupted(args.job)


def start_background(args, lease_fd):
    # FD inheritance transfers the already-acquired lease lock without a race.
    lease = reserve_lease(args)
    for name in ('deploy.log', 'worker.log'):  # rotate only once a new lease exists
        path = STATE / name
        if path.exists():
            path.replace(STATE / (name + '.previous'))
    try:
        child = spawn_worker(worker_command('_worker', '--lease-fd', lease_fd, lease['session']),
                             lease_fd, 'w')
    except BaseException:
        stop_owned(lease)
        raise
    event('后台部署进程已启动（尚未 READY）', pid=child.pid,
          logs='./colab/deploy.sh logs -f', stop='./colab/deploy.sh stop')
    return early_exit_code(child)


# ---- Switch ----------------------------------------------------------------

def verify_switch_owner(session, endpoint, job=None):
    data = current()
    if (not data or data.get('session') != session or data.get('endpoint') != endpoint
            or data.get('state') in STOP_STATES
            or (job and (data.get('switch') or {}).get('job') != job)):
        raise LeaseStopped('切换已不再持有记录中的 VM')
    assignments = server_sessions()
    if (assignments.get(endpoint) != session
            or [e for e, name in assignments.items() if name == session] != [endpoint]):
        raise LeaseStopped('未能确认 VM 端点身份；未改动任何 VM')
    return data


def require_switch_aware_supervision(data):
    # A previous launcher process retains its original Python code in memory.
    # Its non-retained health supervisor may release this VM while infer is down;
    # changing retain here cannot safely fence an already in-flight old probe.
    if not data.get('retain') and (data.get('supervision') or {}).get('switch_aware') is not True:
        raise ValueError('该限时租约没有已确认可感知切换的监督进程（switch-aware supervisor）；'
                         '请正常运行 stop（stop normally），再用更新后的启动器'
                         '（updated launcher）重新 start')


def reserve_switch(args):
    options = deployment_options(args)
    original = current()
    if (not original or original.get('state') not in {READY, SWITCH_FAILED}
            or not common.is_owned_session(original.get('session'))
            or not original.get('endpoint')):
        raise ValueError('switch 需要一个已有的、本工具创建的 READY 或 switch-failed VM')
    require_switch_aware_supervision(original)
    verify_switch_owner(original['session'], original['endpoint'])
    with (STATE / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = current()
        if (not data or data.get('session') != original['session']
                or data.get('endpoint') != original['endpoint']
                or data.get('state') not in {READY, SWITCH_FAILED}):
            raise LeaseStopped('预留切换前租约已变化')
        require_switch_aware_supervision(data)
        if not data.get('retain') and time.time() >= data['deadline']:
            raise ValueError('租约已到期；switch 不能延长或替换它')
        data['state'] = SWITCHING
        data['switch'] = {'job': uuid.uuid4().hex[:12], 'state': SWITCHING,
                          'started': time.time(), 'requested_options': options,
                          'poll_interval': opt(args, 'poll_interval'),
                          'progress': {'phase': 'preparing-switch'}}
        data['progress'] = data['switch']['progress']
        if getattr(args, 'retain', False):
            data['retain'] = True
        _write_state(data)
        return data


def poll_switch_progress(lease, job, deadline):
    # Each job has its own progress file: a previous READY cannot complete this job.
    if not re.fullmatch(r'[0-9a-f]{12}', job):
        raise ValueError('无效的切换任务号')
    remaining = int(deadline - time.time())
    if remaining <= 0:
        raise RuntimeError('已到切换期限')
    params = {'private': common.layout_of(lease)['private'],
              'file': f'switch-{job}-progress.json', 'marker': 'COMPUTE_SWITCH_PROGRESS',
              'placeholder': {'job': job, 'phase': 'starting'}}
    out = remote_task(lease['session'], 'progress', params, timeout=min(120, remaining))
    line = marker_line(out, 'COMPUTE_SWITCH_PROGRESS')
    if line is None:
        raise RuntimeError('缺少结构化切换进度输出')
    progress = json.loads(line)
    if progress.get('job') != job:
        raise RuntimeError('远端切换进度属于另一个任务（another job）')
    return progress


def record_switch_progress(session, job, summary):
    latest = current() or {}
    switch = {**latest.get('switch', {}), 'progress': summary}
    switch_update(session, job, switch=switch, progress=summary)
    return switch


def switch_identity_matches(progress, options):
    effective = progress.get('effective_options')
    return (progress.get('model') == options['model']
            and progress.get('revision') == options['revision']
            and isinstance(effective, dict)
            and all(effective.get(k) == options[k] for k in
                    ('model', 'revision', 'speculative_config', 'attention_backend',
                     'kv_cache_dtype')))


def run_switch(args):
    data = current()
    if not data or not re.fullmatch(r'[0-9a-f]{12}', getattr(args, 'job', '') or ''):
        raise LeaseStopped('无效的切换任务身份')
    session, endpoint, job = data['session'], data.get('endpoint'), args.job
    switch = data.get('switch') or {}
    if switch.get('job') != job or data.get('state') != SWITCHING:
        raise LeaseStopped('切换任务已不再活动')
    options = switch['requested_options']
    try:
        verify_switch_owner(session, endpoint, job)
        switch_update(session, job, switch={**switch, 'worker_pid': os.getpid()})
        event('切换模型：已确认现有 VM，保留租约与 Tailnet', job=job, model=options['model'])
        stage = _stage_switch(data, job, options)
        _kick_switch(data, job, stage)
        return _await_switch(data, job, switch, options)
    except LeaseStopped:
        _record_switch_failure(session, job, 'LeaseStopped')
        event('切换已终止：停止操作或租约身份变更优先', job=job)
        return 1
    except BaseException as exc:
        # A switch never owns VM allocation/release. Even failure leaves stop to
        # the explicit operator or the existing non-retained lease supervisor.
        _record_switch_failure(session, job, type(exc).__name__, failed=time.time())
        event('切换失败，VM 保留；查看日志后可重试 switch', job=job, error_type=type(exc).__name__)
        raise


def _record_switch_failure(session, job, error_type, **extra):
    try:
        latest = current() or {}
        summary = {'phase': 'failed', 'detail': error_type}
        failed = {**latest.get('switch', {}), 'state': SWITCH_FAILED, 'error_type': error_type,
                  'progress': summary, **extra}
        switch_update(session, job, state=SWITCH_FAILED, switch=failed, progress=summary)
    except LeaseStopped:
        pass


def switch_paths(lease, job):
    private = common.layout_of(lease)['private']
    return f'{private}/staging/{job}', f'{private}/switch-{job}.json'


def _stage_switch(data, job, options):
    session, endpoint = data['session'], data['endpoint']
    stage, options_path = switch_paths(data, job)
    with tempfile.TemporaryDirectory(prefix='agent-colab-switch-') as temporary:
        temp = Path(temporary)
        archive = temp / 'source.tar.gz'
        record_switch_progress(session, job, {'phase': 'packing-source'})
        event('切换模型：开始打包当前计算端源码', job=job)
        count = make_archive(ROOT, archive)
        event('切换模型：源码打包完成', job=job, files=count, bytes=archive.stat().st_size)
        local_options = temp / 'options.json'
        local_options.write_text(json.dumps({**options, 'staging_dir': stage,
                                             'vm_layout': common.layout_of(data)}))
        local_options.chmod(0o600)
        verify_switch_owner(session, endpoint, job)
        out = remote_task(session, 'switch_stage', {'stage': stage})
        if 'COMPUTE_SWITCH_STAGE_READY' not in out:
            raise RuntimeError('未确认切换暂存目录')
        uploads = [(archive, f'{stage}/source.tar.gz'),
                   *[(ROOT / 'colab' / name, f'{stage}/{name}') for name in VM_FILES],
                   (local_options, options_path)]
        for local, destination in uploads:
            name = Path(destination).name
            verify_switch_owner(session, endpoint, job)
            record_switch_progress(session, job, {'phase': 'uploading-source', 'detail': name})
            event('切换模型：开始上传', job=job, file=name)
            cli('upload', '-s', session, str(local), destination, timeout=120, capture=True)
            event('切换模型：上传完成', job=job, file=name)
    return stage


def _kick_switch(data, job, stage):
    # Never overwrite the running helper/config. The staged helper owns the
    # stop-infer -> positive GPU vacancy -> merge -> load transaction.
    session = data['session']
    layout = common.layout_of(data)
    verify_switch_owner(session, data['endpoint'], job)
    record_switch_progress(session, job, {'phase': 'starting-switch'})
    out = remote_task(session, 'switch_kick', {
        'private': layout['private'], 'stage': stage, 'job': job,
        'helper': 'compute_vm.py', 'options': switch_paths(data, job)[1],
        'python': f'{layout["app"]}/backend/.venv/bin/python'})
    if 'COMPUTE_SWITCH_STARTED' not in out:
        raise RuntimeError('未确认远端切换进程已启动')
    event('切换模型：远端进程已启动', job=job)


def _await_switch(data, job, switch, options):
    session, endpoint = data['session'], data['endpoint']
    deadline = switch['started'] + options['bootstrap_timeout']

    def fetch():
        verify_switch_owner(session, endpoint, job)
        return poll_switch_progress(data, job, deadline)

    def handle(progress, idle_s):
        summary = {k: progress[k] for k in ('phase', 'detail', 'download') if k in progress}
        event('切换模型：' + PHASE_LABELS.get(progress['phase'], progress['phase']), job=job,
              **summary, elapsed_s=round(time.time() - switch['started']), no_progress_s=idle_s)
        latest = record_switch_progress(session, job, summary)
        if progress['phase'] == 'failed':
            raise RuntimeError('远端切换失败（remote switch failed）；VM 保留，查看日志后重试')
        if progress['phase'] != 'ready':
            return None
        if not switch_identity_matches(progress, options):
            raise RuntimeError('远端模型/选项与请求的切换不一致（identity did not match）')
        latest.update(state=READY, completed=time.time(),
                      effective_options=progress['effective_options'])
        switch_update(session, job, state=READY, switch=latest,
                      model=options['model'], options=progress['effective_options'])
        event('READY：已在原 VM 完成模型切换', job=job, model=options['model'],
              speculative=options['speculative'], stop='./colab/deploy.sh stop')
        return 0

    return follow_progress(fetch, handle, deadline=deadline,
                           stall_timeout=options['stall_timeout'],
                           poll_interval=switch.get('poll_interval', DEFAULTS['poll_interval']),
                           log_prefix='VM switch | ', log_fields={'job': job})


def switch_existing(args, switch_fd):
    data = reserve_switch(args)
    job = data['switch']['job']
    if args.foreground:
        args.job = job
        with interrupts_raise():
            return run_switch(args)
    try:
        child = spawn_worker(worker_command('_switch', '--switch-fd', switch_fd, job),
                             switch_fd, 'a')
    except BaseException:
        _record_switch_failure(data['session'], job, 'LaunchFailed')
        raise
    event('后台模型切换已启动（尚未 READY）', job=job, pid=child.pid,
          logs='./colab/deploy.sh logs -f', stop='./colab/deploy.sh stop')
    return early_exit_code(child)


# ---- Logs ------------------------------------------------------------------

def show_logs(lines, follow):
    path = STATE / 'deploy.log'
    if not path.exists():
        print('还没有部署日志')
        return
    with path.open() as log:
        print(''.join(deque(log, maxlen=lines)), end='', flush=True)
        while follow:
            line = log.readline()
            if line:
                print(line, end='', flush=True)
            else:
                time.sleep(.5)


def show_remote_logs(lines):
    data = current()
    if not data or server_sessions().get(data.get('endpoint')) != data.get('session'):
        raise RuntimeError('无法按记录的会话访问 VM；请查看本机日志')
    job = (data.get('switch') or {}).get('job')
    switching = data.get('state') in {SWITCHING, SWITCH_FAILED}
    log_name = (f'switch-{job}.log' if switching and job and re.fullmatch(r'[0-9a-f]{12}', job)
                else 'bootstrap.log')
    layout = common.layout_of(data)
    out = remote_task(data['session'], 'sanitized_text',
                      {'mode': 'log', 'private': layout['private'], 'app': layout['app'],
                       'helper': layout['helper'], 'log': log_name, 'max_bytes': 65536},
                      timeout=30)
    text = marker_line(out, 'COMPUTE_TEXT')
    if text is None:
        raise RuntimeError('无法取得 VM 脱敏日志')
    print('\n'.join(base64.b64decode(text, validate=True).decode().splitlines()[-lines:]))


# ---- Command line ----------------------------------------------------------

def bounded(kind, name):
    def parse(text):
        try:
            return check_limit(name, kind(text))
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None
    return parse


def build_parser():
    parser = argparse.ArgumentParser(prog='deploy.sh', description='Colab 计算端租约管理')
    commands = parser.add_subparsers(dest='command', required=True, metavar='命令')

    def deployment(p):
        p.add_argument('--model', default=DEFAULTS['model'], help=f'已注册的 CUDA 模型（默认 {MODEL}）；见 models')
        p.add_argument('--speculative', choices=['off', 'mtp'], default=None,
                       help='CUDA 投机解码模式（默认：注册表标明支持 MTP 的模型开 mtp，'
                            '如 Qwen3.8-27B；其余关，如 gemma-4 系列）')
        p.add_argument('--draft-tokens', type=bounded(int, 'draft_tokens'),
                       help=f'MTP draft token 数（1–5，默认 {MTP_DRAFT_TOKENS}；需 mtp）')
        p.add_argument('--attention-backend', choices=list(ATTENTION_BACKENDS), default='auto')
        p.add_argument('--kv-cache-dtype', choices=KV_CACHE_DTYPES, default='auto')
        for name, kind, text in [('max_model_len', int, '最大上下文长度'),
                                 ('gpu_memory', float, 'vLLM 显存占用比例'),
                                 ('stall_timeout', int, '无可观测进度的最长秒数'),
                                 ('bootstrap_timeout', int, '部署最长秒数'),
                                 ('poll_interval', int, '进度查询间隔秒数')]:
            p.add_argument('--' + name.replace('_', '-'), type=bounded(kind, name),
                           default=DEFAULTS[name], help=f'{text}（默认 {DEFAULTS[name]}）')

    def internal(name, fd_flag):
        p = commands.add_parser(name)
        p.add_argument(fd_flag, type=int, required=True)
        p.add_argument('--job', required=True)

    commands.add_parser('prepare', help='打包检查；不读密钥、不创建 VM')
    commands.add_parser('models', help='列出 config.yaml 中钉了 revision 的 CUDA 模型')
    p = commands.add_parser('start', help='创建 VM 并部署计算端（默认后台）')
    deployment(p)
    p.add_argument('--minutes', type=bounded(int, 'minutes'), default=DEFAULTS['minutes'],
                   help='限时租约时长（1–240 分钟）')
    p.add_argument('--auth-key', type=Path, default=DEFAULTS['auth_key'])
    p.add_argument('--hostname', default=HOSTNAME, help='固定的 MagicDNS 机器名')
    p.add_argument('--retain', action='store_true', help='READY 后一直保留，直到手动 stop')
    p.add_argument('--foreground', action='store_true', help='前台运行；Ctrl+C 请求回收')
    p.add_argument('--health-interval', type=bounded(int, 'health_interval'),
                   default=DEFAULTS['health_interval'], help='READY 后健康检查间隔秒数')
    p = commands.add_parser('switch', help='在现有 VM 上切换模型或推理选项')
    deployment(p)
    p.add_argument('--retain', action='store_true', help='切换的同时改为手动保留')
    p.add_argument('--foreground', action='store_true')
    p = commands.add_parser('watch', help='为已有 READY VM 重新挂上监督；不新建 VM')
    p.add_argument('--retain', action='store_true')
    p.add_argument('--health-interval', type=bounded(int, 'health_interval'),
                   default=DEFAULTS['health_interval'])
    p = commands.add_parser('status', help='租约、服务端 VM、本机可见遗留节点')
    p.add_argument('--hostname', default=HOSTNAME)
    p = commands.add_parser('stop', help='注销节点、释放本工具的 VM 并核对服务端')
    p.add_argument('--confirm-unallocated', action='store_true',
                   help='人工已核对 colab sessions 没有本工具的 VM：允许把无端点的租约记为 stopped'
                        '（服务端仍有本会话或身份不明的 VM 时照旧拒绝）')
    p = commands.add_parser('logs', help='查看部署日志')
    p.add_argument('--lines', type=bounded(int, 'lines'), default=DEFAULTS['lines'])
    p.add_argument('--remote', action='store_true', help='拉取 VM 脱敏日志末尾（快照）')
    p.add_argument('-f', '--follow', action='store_true', help='持续跟随；Ctrl+C 只退出查看器')
    internal('_worker', '--lease-fd')
    internal('_watch', '--lease-fd')
    internal('_switch', '--switch-fd')
    return parser


def inherited_lock(fd, name):
    expected = (STATE / name).stat()
    inherited = os.fstat(fd)
    if (expected.st_dev, expected.st_ino) != (inherited.st_dev, inherited.st_ino):
        raise ValueError(f'继承的 {name} 不匹配')
    os.set_inheritable(fd, False)
    return os.fdopen(fd, 'a')


def dispatch(args):
    if args.command == 'prepare':
        private_dir(STATE)
        print('源码文件数：', make_archive(ROOT, STATE / 'source.tar.gz'))
        print('未读取任何凭据；未创建 VM。')
    elif args.command == 'models':
        for repo, entry in registered_models().items():
            mtp = (entry.get('cuda_deployment') or {}).get('supports_mtp')
            print(repo, '@', entry['revision'], 'MTP=' + ('yes' if mtp else 'no'))
    elif args.command == 'logs':
        if args.remote:
            if args.follow:
                raise ValueError('--remote 是一次性快照；持续查看本机进度请用 logs -f')
            show_remote_logs(args.lines)
        else:
            show_logs(args.lines, args.follow)
    elif args.command == '_switch':
        with inherited_lock(args.switch_fd, 'switch.lock'), interrupts_raise():
            return run_switch(args)
    elif args.command in {'_worker', '_watch'}:
        with inherited_lock(args.lease_fd, 'lease.lock'):
            if args.command == '_watch':
                return run_watch_worker(args, args.lease_fd)
            start(args)
    elif args.command == 'start':
        deployment_options(args)  # Reject unsupported model/options before detaching.
        private_dir(STATE)
        with (STATE / 'lease.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            previous = current()
            if previous and previous.get('state') != STOPPED:
                raise ValueError('上一个租约尚未确认停止；先运行 status / stop')
            if not args.foreground:
                return start_background(args, lock.fileno())
            start(args)
    elif args.command == 'watch':
        private_dir(STATE)
        with (STATE / 'lease.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return watch_existing(args, lock.fileno())
    elif args.command == 'switch':
        deployment_options(args)
        private_dir(STATE)
        with (STATE / 'switch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return switch_existing(args, lock.fileno())
    elif args.command == 'stop':
        data = snapshot()
        if data is None and (STATE / 'current.json').exists():
            raise RuntimeError('状态文件已损坏，无法确定要释放哪台 VM；用 status / colab sessions '
                               '核对本工具的会话后在 Colab 控制台释放，再删除该文件')
        if data and (data.get('endpoint') or data.get('state') != STOPPED):
            # The operator's confirmation applies to this invocation only; it is not
            # persisted, so a later plain `stop` does not inherit it.
            stop_owned(data, operator_confirmed=getattr(args, 'confirm_unallocated', False))
        else:
            print('没有本工具持有的活动租约')
    elif args.command == 'status':
        print(json.dumps(supervisor_status(snapshot()), indent=2, ensure_ascii=False))
        print(json.dumps(inspect_resources(args.hostname), indent=2, ensure_ascii=False))
        version = colab_cli_version()
        print('Colab CLI', version, '（已验证）' if version in VERIFIED_COLAB_CLI else '（未验证）')
        cli('usage')
    else:
        raise ValueError(f'未实现的命令 {args.command}')
    return 0


def main(argv=None):
    global ECHO
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    ECHO = not args.command.startswith('_')
    try:
        import yaml  # noqa: F401  (registered_models)
    except ImportError:
        event('ERROR：缺少部署依赖 PyYAML；请通过 ./colab/deploy.sh 运行，而不是裸 python')
        return 1
    try:
        return dispatch(args) or 0
    except KeyboardInterrupt:
        return 130
    except BlockingIOError:
        event('ERROR：另一个部署 / 监督 / 切换进程正持有同一把锁；先用 status 查看')
        return 1
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        # No exception repr for subprocesses: it may contain remote output.
        fields = {'reason': str(exc) if isinstance(exc, (ValueError, RuntimeError))
                  else type(exc).__name__}
        if exc.__context__ is not None:
            fields['context_type'] = type(exc.__context__).__name__
        event('ERROR', **fields)
        return 1


if __name__ == '__main__':
    sys.exit(main())
