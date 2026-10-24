#!/usr/bin/env python3
"""VM 侧部署 helper：只负责环境准备与模型配置，计算服务仍只由 devctl 启动。

两种调用：
  python3 compute_vm.py                      首次部署（由 vm_tasks/bootstrap_kick 启动）
  <venv>/python compute_vm.py switch ...     在已部署的 VM 上切换模型（暂存目录中的新副本）
VM 路径来自租约记录的 layout（compute_common），因此同一份代码也能切换旧布局的 VM。
"""
import argparse
from collections import deque
import fcntl
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import select
import shutil
import signal
import subprocess
import tarfile
import time
import urllib.error
import urllib.request


def _load_common():
    spec = importlib.util.spec_from_file_location(
        'compute_common', Path(__file__).resolve().with_name('compute_common.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = _load_common()

PRIVATE = Path(common.LAYOUT['private'])
APP = Path(common.LAYOUT['app'])
MODEL = 'RedHatAI/gemma-4-26B-A4B-it-NVFP4'
# Pinned like every other dependency: an exact release plus its published digest.
TAILSCALE_VERSION = '1.104.2'
TAILSCALE_SHA256 = '512f613ed7d04fc7282f7dc5bd76aa105aae28168fbb9357eb749ebcbc29f0ae'
UV_VERSION = '0.13.1'
VM_PYTHON = '3.12'
LOG_TAIL_LINES = 16
FAILURE_TAIL_CHARS = 2500
SECRET_VALUES = []
LAST_FAILURE = ''
PHASE = ''
PHASE_STARTED = 0
ACTIVITY_SEQ = 0
LOG_SEQ = 0
LOG_TAIL = deque(maxlen=LOG_TAIL_LINES)
DETAIL = ''
JOB = None


def use_layout(layout):
    global PRIVATE, APP
    PRIVATE, APP = Path(layout['private']), Path(layout['app'])


def known_layout_for(private):
    for layout in (common.LAYOUT, common.LEGACY_LAYOUT):
        if Path(layout['private']) == Path(private):
            return layout
    raise ValueError('未知的 VM 私有目录')


def venv_python():
    return str(APP / 'backend/.venv/bin/python')


def tailscale_bin(name='tailscale'):
    return str(PRIVATE / 'bin' / name)


# ---- Logging, redaction and progress ---------------------------------------

def redact(text):
    return common.redact(text, SECRET_VALUES)


def tail_bytes(path, limit=65536):
    with path.open('rb') as log:
        log.seek(0, 2)
        size = log.tell()
        log.seek(max(0, size - limit))
        raw = log.read(limit)
    return raw.partition(b'\n')[2] if size > limit else raw


def save_failure_diagnostic():
    # devctl prints only the final traceback, often omitting a worker's root cause.
    # Preserve sanitized service logs before the host releases this ephemeral VM.
    sections = [LAST_FAILURE]
    for path in (APP / 'logs/rag-compute.log', APP / 'logs/infer.log'):
        if path.is_file():
            sections.append(path.name + '\n' + redact(tail_bytes(path).decode(errors='replace')))
    target = PRIVATE / 'failure-diagnostic.log'
    target.write_text('\n\n'.join(sections))
    target.chmod(0o600)


def progress(phase, **fields):
    global PHASE, PHASE_STARTED, ACTIVITY_SEQ
    changed = phase != PHASE
    if changed:
        PHASE, PHASE_STARTED = phase, time.time()
        ACTIVITY_SEQ += 1
    value = {'phase': phase, 'heartbeat_at': time.time(), 'phase_started': PHASE_STARTED,
             'activity_seq': ACTIVITY_SEQ, 'detail': DETAIL, 'log_tail': list(LOG_TAIL), **fields}
    identity = PRIVATE / 'tailnet.json'
    if identity.exists():
        value['tailnet'] = json.loads(identity.read_text())
    if JOB:
        value['job'] = JOB
    target = PRIVATE / (f'switch-{JOB}-progress.json' if JOB else 'progress.json')
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(target)
    if changed:
        print(json.dumps(value), flush=True)


def log_line(line):
    global LOG_SEQ, ACTIVITY_SEQ, DETAIL
    line = redact(line).strip()
    if not line.strip('. '):
        return
    line = line[-1800:]
    LOG_SEQ += 1
    ACTIVITY_SEQ += 1
    DETAIL = line
    LOG_TAIL.append((LOG_SEQ, line))
    print(line, flush=True)


def service_lines(offsets):
    for name in ('infer.log',) if JOB else ('infer.log', 'rag-compute.log'):
        path = APP / 'logs' / name
        if not path.exists():
            continue
        size = path.stat().st_size
        offset = offsets.get(name, 0)
        if size < offset:
            offset = 0
        with path.open('rb') as log:
            # Bound memory even if a dependency emits an enormous line.
            log.seek(max(offset, size - 65536))
            raw = log.read(65536)
            if size - offset > 65536:
                raw = raw.partition(b'\n')[2]
            complete, separator, partial = raw.replace(b'\r', b'\n').rpartition(b'\n')
            if separator:
                offsets[name] = log.tell() - len(partial)
                for line in complete.decode(errors='replace').splitlines():
                    log_line(name + ' | ' + line)


def run(*args, timeout=1800, env=None, watch_services=False):
    """Run a command with live, redacted log relay; bounded; kills its own group on exit."""
    global LAST_FAILURE, ACTIVITY_SEQ
    started = time.monotonic()
    last_report = 0
    pending = b''
    discard_line = False
    offsets = {}
    downloaded = None
    proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)
    try:
        while True:
            if JOB:
                check_not_stopped()
            if time.monotonic() - started > timeout:
                raise subprocess.TimeoutExpired(args, timeout)
            ready, _, _ = select.select([proc.stdout], [], [], .2)
            if ready:
                chunk = os.read(proc.stdout.fileno(), 8192)
                for part in chunk.replace(b'\r', b'\n').splitlines(keepends=True):
                    pending += part
                    if len(pending) > 65536:
                        pending, discard_line = b'', True
                    if part.endswith(b'\n'):
                        if not discard_line:
                            log_line(pending.decode(errors='replace'))
                        pending, discard_line = b'', False
                if not chunk and proc.poll() is not None:
                    if pending and not discard_line:
                        log_line(pending.decode(errors='replace'))
                    break
            fields = {}
            if watch_services:
                service_lines(offsets)
            download_file = PRIVATE / 'download.json'
            if PHASE == 'downloading-model' and download_file.exists():
                fields['download'] = json.loads(download_file.read_text())
                count = fields['download'].get('bytes_downloaded')
                if count != downloaded:
                    downloaded = count
                    ACTIVITY_SEQ += 1
            if time.monotonic() - last_report >= 5:
                progress(PHASE, **fields)
                last_report = time.monotonic()
        if watch_services:
            service_lines(offsets)
        if proc.returncode:
            LAST_FAILURE = '\n'.join(text for _, text in LOG_TAIL)[-FAILURE_TAIL_CHARS:]
            raise subprocess.CalledProcessError(proc.returncode, args)
    finally:
        if proc.poll() is None:
            # Only the process group created by this call belongs to us.
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        proc.stdout.close()


def json_get(url, key=None, payload=None):
    headers = {'Authorization': 'Bearer ' + key} if key else {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def infer_url(path):
    return f'http://127.0.0.1:{common.INFER_PORT}{path}'


def rag_url(path):
    return f'http://127.0.0.1:{common.RAG_PORT}{path}'


# ---- Private files ---------------------------------------------------------

def atomic_write(path, text):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text)
    temporary.chmod(0o600)
    temporary.replace(path)


def service_environment():
    """Read our own literal .env.local; register its secrets for redaction."""
    values = common.parse_env((APP / '.env.local').read_text())
    SECRET_VALUES.extend(common.secret_values(values))
    env = dict(os.environ, **values)
    env['PATH'] = str(APP / 'backend/.venv/bin') + os.pathsep + env.get('PATH', os.defpath)
    return values, env


def write_service_environment(values):
    atomic_write(APP / '.env.local', common.format_env(values))


def check_not_stopped():
    if (PRIVATE / 'stop.requested').exists():
        raise RuntimeError('已收到显式 stop 请求（stop requested）；拒绝启动推理')


def extract_source(archive_path, destination):
    with tarfile.open(archive_path) as archive:
        for item in archive.getmembers():
            path = Path(item.name)
            if not item.isfile() or path.is_absolute() or '..' in path.parts:
                raise ValueError('源码包含不安全的条目（unsafe archive member）')
        archive.extractall(destination, filter='data')


# ---- Tailnet ---------------------------------------------------------------

def sha256_of(path):
    digest = hashlib.sha256()
    with path.open('rb') as data:
        for block in iter(lambda: data.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def install_tailscale():
    """Install the pinned static release into PRIVATE/bin; never run a remote script."""
    if platform.machine() != 'x86_64':
        raise RuntimeError('Tailscale 静态包只钉了 amd64')
    name = f'tailscale_{TAILSCALE_VERSION}_amd64'
    archive = PRIVATE / f'{name}.tgz'
    run('curl', '-fsSL', f'https://pkgs.tailscale.com/stable/{name}.tgz', '-o', str(archive),
        timeout=180)
    if sha256_of(archive) != TAILSCALE_SHA256:
        raise RuntimeError('Tailscale 安装包校验和不匹配；拒绝安装')
    target = PRIVATE / 'bin'
    target.mkdir(mode=0o700, exist_ok=True)
    with tarfile.open(archive) as bundle:
        for binary in ('tailscale', 'tailscaled'):
            source = bundle.extractfile(f'{name}/{binary}')
            if source is None:
                raise RuntimeError(f'Tailscale 安装包缺少 {binary}')
            (target / binary).write_bytes(source.read())
            (target / binary).chmod(0o700)
    archive.unlink()


def tailscaled_command(socket):
    # mem: explicitly registers an ephemeral node, even with a reusable regular
    # auth key. Logout removes it; abrupt loss falls back to server expiry.
    return [tailscale_bin('tailscaled'), '--tun=userspace-networking', '--state=mem:',
            '--statedir=' + str(PRIVATE), '--socket=' + socket]


def record_tailnet(socket, expected_dns):
    result = subprocess.run([tailscale_bin(), '--socket=' + socket, 'status', '--json'],
                            check=True, capture_output=True, text=True, timeout=20)
    status = json.loads(result.stdout)
    node = status.get('Self') or {}
    identity = {'id': node.get('ID'), 'dns_name': node.get('DNSName', '').rstrip('.'),
                'ips': node.get('TailscaleIPs') or []}
    (PRIVATE / 'tailnet.json').write_text(json.dumps(identity))
    if (status.get('BackendState') != 'Running' or not identity['id']
            or identity['dns_name'] != expected_dns):
        raise ValueError('Tailnet 身份或固定域名不符；拒绝带后缀的名字')
    addresses = [ip for ip in identity['ips'] if ipaddress.ip_address(ip).version == 4]
    if len(addresses) != 1:
        raise ValueError('缺少 Tailnet IPv4 身份')
    return addresses[0]


def join_tailnet(network, key):
    install_tailscale()
    socket = str(PRIVATE / 'ts.sock')
    with (PRIVATE / 'tailscaled.log').open('w') as log:
        subprocess.Popen(tailscaled_command(socket), stdout=log, stderr=log,
                         start_new_session=True)
    for _ in range(40):
        if Path(socket).exists():
            break
        time.sleep(.25)
    keyfile = PRIVATE / 'tailnet.authkey'
    keyfile.write_text(key)
    keyfile.chmod(0o600)
    try:
        run(tailscale_bin(), '--socket=' + socket, 'up', '--auth-key=file:' + str(keyfile),
            '--hostname=' + network['hostname'], '--accept-dns=false', timeout=60)
    finally:
        keyfile.unlink(missing_ok=True)
    return record_tailnet(socket, network['expected_dns'])


# ---- Model configuration (shared by bootstrap and switch) ------------------

def configure_model(options, registry=None):
    import yaml
    path = APP / 'config.yaml'
    config = yaml.safe_load(path.read_text())
    infer = config['inference']
    if registry is not None:
        infer['model_registry'] = registry
    entry = next((e for e in infer['model_registry']
                  if e['repo'] == options['model'] and e.get('revision') == options['revision']),
                 None)
    if not entry:
        raise ValueError('模型/revision 未注册')
    spec = options.get('speculative_config')
    if spec is not None and (not (entry.get('cuda_deployment') or {}).get('supports_mtp')
                             or spec.get('method') != 'mtp'
                             or type(spec.get('num_speculative_tokens')) is not int
                             or not 1 <= spec['num_speculative_tokens'] <= 5):
        raise ValueError('不支持的 MTP 配置')
    infer.update(mode='remote', backend='cuda')
    infer['cuda'].update(max_model_len=options['max_model_len'], max_concurrency=1,
                         max_num_seqs=1,
                         gpu_memory_utilization=options['gpu_memory_utilization'],
                         enforce_eager=False, speculative_config=spec,
                         attention_backend=options.get('attention_backend', 'auto'),
                         kv_cache_dtype=options.get('kv_cache_dtype', 'auto'))
    atomic_write(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    atomic_write(APP / '.model-selection', json.dumps({'repo': options['model']}))


def apply_model(options, values, env, fetch_script, *, registry=None, **progress_fields):
    """configure → record the service environment → download (verified revision)."""
    global MODEL
    MODEL = options['model']
    progress('configuring-model')
    configure_model(options, registry)
    values.update(INFER_MODEL_REPO=MODEL, INFER_QUANT=options['quantization'],
                  INFER_SPEC_CONFIG=json.dumps(options.get('speculative_config')),
                  INFER_KV_DTYPE=options.get('kv_cache_dtype', 'auto'))
    write_service_environment(values)
    env.update(values)
    progress('downloading-model', **progress_fields)
    run(venv_python(), str(fetch_script), timeout=options['bootstrap_timeout'], env=env)


def verify_inference(health, options):
    expected = {key: options.get(key, default) for key, default in
                [('speculative_config', None), ('attention_backend', 'auto'),
                 ('kv_cache_dtype', 'auto')]}
    if (health.get('status') != 'ok' or health.get('service') != common.INFER_SERVICE
            or health.get('model_repo') != options['model']
            or health.get('model_revision') != options['revision']
            or health.get('inference_options') != expected):
        raise RuntimeError('推理服务的模型/revision/选项与请求不一致（mismatch）')


def start_compute_services(env):
    # One generation process owns the GPU. Retrieval's small embedding/reranking
    # models run on the VM CPU, through the same devctl entry points.
    rag_env = dict(env, CUDA_VISIBLE_DEVICES='')
    run('bash', str(APP / 'devctl.sh'), 'start', 'rag-compute',
        timeout=1900, env=rag_env, watch_services=True)
    run('bash', str(APP / 'devctl.sh'), 'start', 'infer',
        timeout=1900, env=env, watch_services=True)


# ---- First deployment ------------------------------------------------------

def bootstrap_runtime():
    progress('extracting')
    APP.mkdir(mode=0o700, exist_ok=False)
    extract_source(PRIVATE / 'source.tar.gz', APP)
    progress('installing-python')
    # Colab's system Python lacks working ensurepip; uv creates/seeds the venv
    # without depending on Debian's optional python3-venv package.
    run('python3', '-m', 'pip', 'install', '--disable-pip-version-check',
        '--target', str(PRIVATE / 'tools'), f'uv=={UV_VERSION}', timeout=180)
    uv = str(PRIVATE / 'tools/bin/uv')
    python_env = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(APP / '.python'))
    run(uv, 'venv', '--seed', '--python', VM_PYTHON, str(APP / 'backend/.venv'),
        timeout=180, env=python_env)
    progress('installing-dependencies')
    run(uv, 'pip', 'install', '--python', venv_python(), '-r', str(PRIVATE / 'requirements.txt'))
    # Same isolated CUDA environment, no second inference engine or duplicate packages.
    (APP / 'rag/.venv').symlink_to(APP / 'backend/.venv', target_is_directory=True)


def runtime_environment(credentials):
    env = os.environ.copy()
    # JIT helpers (notably FlashInfer's ninja subprocess) resolve executables via
    # PATH even when devctl launches the venv Python by absolute path.
    env['PATH'] = str(APP / 'backend/.venv/bin') + os.pathsep + env.get('PATH', os.defpath)
    env.update(HF_HOME=str(APP / 'backend/models/.hf'), HF_HUB_DISABLE_XET='1',
               HF_HUB_DISABLE_TELEMETRY='1', INFER_KV_STORE_DISABLE='1',
               COMPUTE_VM_PRIVATE=str(PRIVATE), COMPUTE_VM_APP=str(APP))
    if credentials.get('HF_TOKEN'):
        env['HF_TOKEN'] = credentials['HF_TOKEN']
    return env


def bootstrap_main():
    network = json.loads((PRIVATE / 'network.json').read_text())
    if Path(network['vm_layout']['private']) != PRIVATE:
        raise ValueError('部署配置的 VM 布局与 helper 所在目录不符')
    os.umask(0o077)
    os.environ['PYTHONUNBUFFERED'] = '1'
    credentials = json.loads((PRIVATE / 'credentials.json').read_text())
    SECRET_VALUES.extend(credentials.values())
    (PRIVATE / 'credentials.json').unlink()
    bootstrap_runtime()
    env = runtime_environment(credentials)
    progress('checking-cuda-runtime')
    # Do not inherit the base image's incompatible torchaudio/torch stack.
    run(venv_python(), '-c', 'import torch, torchvision, vllm; '
        'from transformers import AutoProcessor; print("CUDA runtime imports verified")', env=env)
    run('ninja', '--version', env=env)
    progress('joining-tailnet')
    host = join_tailnet(network, credentials.pop('TS_AUTHKEY'))
    # Secrets are literal assignments, not executable input; the file is private.
    values = {k: credentials[k] for k in common.REQUIRED_SECRETS}
    values.update(INFER_BACKEND='cuda', INFER_KV_STORE_DISABLE='1',
                  INFER_REMOTE_HOST='127.0.0.1', RAG_COMPUTE_HOST='127.0.0.1',
                  HF_HOME=env['HF_HOME'], HF_HUB_DISABLE_XET='1',
                  RAG_TIMEOUT='900', INFER_TIMEOUT='900')
    if credentials.get('HF_TOKEN'):
        values['HF_TOKEN'] = credentials['HF_TOKEN']
    apply_model(network, values, env, PRIVATE / 'compute_fetch.py', host=host)
    progress('loading-models', host=host)
    # userspace Tailscale forwards incoming tailnet traffic to the matching loopback port.
    # No public listener, no SSH enablement, no GPU/tunnel keepalive workload.
    start_compute_services(env)
    progress('checking-authentication', host=host)
    verify_inference(json_get(infer_url('/health')), network)
    json_get(infer_url('/metrics'), credentials['INFER_API_KEY'])
    rag = json_get(rag_url('/health'))
    if rag.get('device') != 'cpu':
        raise RuntimeError('RAG 计算必须使用 CPU；GPU 归推理独占')
    json_get(rag_url('/api/v1/tokenize'), credentials['RAG_COMPUTE_API_KEY'],
             {'texts': ['界面回归验证'], 'model_type': 'embedding'})
    run(venv_python(), '-m', 'pip', 'freeze')
    progress('ready', host=host, model=MODEL, revision=network['revision'],
             effective_options=network, rag_device=rag.get('device'), rag_tier=rag.get('tier'))


# ---- Switch ----------------------------------------------------------------

def check_idle(key):
    # Connection refusal permits recovery after a failed load. Other failures fail closed.
    try:
        health = json_get(infer_url('/health'))
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return
        raise
    if health.get('service') != common.INFER_SERVICE:
        raise RuntimeError('推理端口被未知服务占用')
    metrics = json_get(infer_url('/metrics'), key)
    if type(metrics.get('active_gpu_work')) is not int or metrics['active_gpu_work'] != 0:
        raise RuntimeError('推理服务忙或活动状态未知；请等请求结束后重试')


def require_free_gpu():
    output = subprocess.check_output(
        ['nvidia-smi', '--query-compute-apps=pid,process_name,used_gpu_memory',
         '--format=csv,noheader'], text=True, timeout=20).strip()
    if output:
        raise RuntimeError('停止推理后 GPU 仍被占用（GPU still occupied）；拒绝加载新模型')
    log_line('GPU occupancy verified: no compute process remains')


def overlay_inference(source):
    # The running RAG service and its source/configuration remain in place.
    exact = {'devctl.sh', 'scripts/lib_edge.sh', 'backend/scripts/fetch_model.py'}
    for file in source.rglob('*'):
        if not file.is_file():
            continue
        relative = file.relative_to(source).as_posix()
        if relative in exact or (relative.endswith('.py')
                                 and relative.startswith(('backend/framework/', 'inference/'))):
            target = APP / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, target)


RAG_IDENTITY = ('status', 'device', 'tier', 'embed_repo', 'rerank_repo', 'embed_dim', 'dtype')


def switch_main(options_path, job):
    global JOB
    if not re.fullmatch(r'[0-9a-f]{12}', job):
        raise ValueError('无效的切换任务号')
    JOB = job
    os.umask(0o077)
    if Path(options_path) != PRIVATE / f'switch-{job}.json':
        raise ValueError('切换选项路径不符')
    with (PRIVATE / 'inference-switch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_not_stopped()
        options = json.loads(Path(options_path).read_text())
        stage = PRIVATE / 'staging' / job
        if options.get('staging_dir') != str(stage) or stage.is_symlink():
            raise ValueError('暂存目录（staging）不符')
        if Path((options.get('vm_layout') or {}).get('private', '')) != PRIVATE:
            raise ValueError('切换选项的 VM 布局与当前 VM 不符')
        progress('preparing-switch')
        source = stage / 'source'
        source.mkdir(mode=0o700, exist_ok=False)
        extract_source(stage / 'source.tar.gz', source)
        import yaml
        registry = yaml.safe_load((source / 'config.yaml').read_text())['inference']['model_registry']
        if not any(e['repo'] == options['model'] and e.get('revision') == options['revision']
                   for e in registry):
            raise ValueError('切换目标模型/revision 未注册')
        values, env = service_environment()
        env.update(COMPUTE_VM_PRIVATE=str(PRIVATE), COMPUTE_VM_APP=str(APP))
        rag_before = json_get(rag_url('/health'))
        if rag_before.get('device') != 'cpu' or rag_before.get('status') != 'healthy':
            raise RuntimeError('RAG 健康状态不可用或 GPU 归属未隔离')
        check_idle(values['INFER_API_KEY'])
        check_not_stopped()
        progress('stopping-inference')
        run('bash', str(APP / 'devctl.sh'), 'stop', 'infer', timeout=90, env=env)
        require_free_gpu()
        check_not_stopped()
        overlay_inference(source)
        apply_model(options, values, env, stage / 'compute_fetch.py', registry=registry)
        check_not_stopped()
        require_free_gpu()
        progress('loading-models')
        run('bash', str(APP / 'devctl.sh'), 'start', 'infer', timeout=1900, env=env,
            watch_services=True)
        check_not_stopped()
        progress('checking-authentication')
        verify_inference(json_get(infer_url('/health')), options)
        json_get(infer_url('/metrics'), values['INFER_API_KEY'])
        rag_after = json_get(rag_url('/health'))
        if any(rag_after.get(k) != rag_before.get(k) for k in RAG_IDENTITY):
            raise RuntimeError('切换期间 RAG 身份发生变化（RAG identity）')
        host = json.loads((PRIVATE / 'tailnet.json').read_text())['dns_name']
        check_not_stopped()
        progress('ready', model=MODEL, revision=options['revision'], host=host,
                 effective_options={k: v for k, v in options.items()
                                    if k not in {'staging_dir', 'vm_layout'}})


def main(argv=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command')
    switch = commands.add_parser('switch')
    switch.add_argument('--options', required=True)
    switch.add_argument('--job', required=True)
    args = parser.parse_args(argv)
    if args.command == 'switch':
        use_layout(known_layout_for(Path(args.options).parent))
        switch_main(args.options, args.job)
    else:
        # The bootstrap helper lives in the private directory it deploys from.
        use_layout(known_layout_for(Path(__file__).resolve().parent))
        bootstrap_main()


if __name__ == '__main__':
    try:
        main()
    except BaseException as exc:
        # Intentionally omit exception text/tracebacks: dependency errors can echo URLs.
        try:
            save_failure_diagnostic()
        except OSError:
            pass  # Diagnostic collection must not prevent the host's cleanup path.
        try:
            progress('failed', error_type=type(exc).__name__,
                     diagnostic=redact(str(exc)) if isinstance(exc, (ValueError, RuntimeError))
                     else LAST_FAILURE)
        except OSError:
            pass
        raise SystemExit(1)
