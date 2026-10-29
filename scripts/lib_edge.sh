#!/bin/bash
# =======================================================
# lib_edge.sh — edge 启动逻辑的单一真相（R5-4 / D.13）
#
# 「启什么 / 什么顺序 / 怎么算就绪」只写在这里。两个调用方：
#   - devctl.sh   （仓库布局：source 本文件，dev/all-in-one/edge/gpu 全部走这里的机制函数）
#   - macOS app   （bundle 布局：SidecarManager 直接执行 `lib_edge.sh up|down|status`）
# Swift 侧**只做进程监护**（崩溃重启 / 日志呈现 / 端口冲突 UX / 随 app 收摊），
# 不得在 Swift 里重写启动顺序与就绪判据——那些是多起事故换来的（§S.3：RAG 未就绪
# 即拒启 backend，因工具探活只在 backend 启动期发生一次）。
#
# 布局参数（调用方在 source / 执行前以环境变量注入；两种布局的差异**只**在这里）：
#   EDGE_BACKEND_DIR     backend 代码目录       仓库: <root>/backend   bundle: Resources/backend
#   EDGE_BACKEND_PY      backend 解释器         仓库: backend/.venv/bin/python
#                                               bundle: Resources/python/bin/python3.13（R5-3 PBS）
#   EDGE_SEARX_DIR       searxng 部署目录       仓库: <root>/searxng   bundle: Resources/searxng
#   EDGE_SEARX_PY        searx 解释器           仓库: searxng/.venv/bin/python
#   EDGE_SEARX_SETTINGS  settings.yml 路径      仓库: searxng/settings.yml
#                                               bundle: $AGENT_DATA_DIR/searxng-settings.yml
#                                               （bundle 只读——首启生成的配置必须落数据根）
#   EDGE_LOG_DIR         日志与 pid 目录        仓库: <root>/logs      bundle: $AGENT_DATA_DIR/logs
#   EDGE_CONFIG          config.yaml 路径       仓库: <root>/config.yaml
#   EDGE_SETUP_HINT      缺解释器时的提示（可选） 仓库: "请先运行: ./devctl.sh setup"
#
# 可选（RAG 数据面，B2c 拆分——rag.host 为本机时它属于 edge 编队，先于 backend）：
#   EDGE_RAG_DIR         rag 代码目录           仓库: <root>/rag       bundle: Resources/rag
#   EDGE_RAG_PY          rag 解释器             仓库: rag/.venv/bin/python
#                                               bundle: Resources/python/bin/python3.13
#                                               （数据面不 import torch，backend 运行时可承载）
#   EDGE_RAG_DATA_DIR    rag 数据根（可选）      仓库: 空（缺省 rag/ 本目录）
#                                               bundle: $AGENT_DATA_DIR/rag（bundle 只读）
#   未注入 EDGE_RAG_PY = 本布局无 rag 承载：rag.host 指远端时无影响；指本机时
#   start_backend 的 RAG 就绪门会拦下并说明。
#
# 密钥不在本文件的职责内：devctl 在 source 前已把 .env.local 灌进环境；app 从
# Keychain 取出后注入子进程环境（R5-6）。本文件只透传，不读任何密钥文件。
# =======================================================

# ---- 参数校验：布局变量缺一不可（静默猜布局 = 把两种布局的差异重新藏起来）----
edge_require_env() {
    local missing="" v
    for v in EDGE_BACKEND_DIR EDGE_BACKEND_PY EDGE_SEARX_DIR EDGE_SEARX_PY \
             EDGE_SEARX_SETTINGS EDGE_LOG_DIR EDGE_CONFIG; do
        eval "[ -n \"\${$v:-}\" ]" || missing="$missing $v"
    done
    if [ -n "$missing" ]; then
        # ${missing} 的花括号不能省：紧跟的是全角括号，bash 3.2 会把它的首字节吞进变量名
        echo "[lib_edge] 缺少布局参数:${missing}（见 lib_edge.sh 头部注释）" >&2
        return 1
    fi
}

# ---- 输出（调用方已定义则沿用其配色/前缀，未定义则给纯文本兜底）----
if ! type info >/dev/null 2>&1; then
    info()  { echo "[edge] $*"; }
    warn()  { echo "[edge] $*" >&2; }
    fail()  { echo "[edge] $*" >&2; }
fi

# ---- 端口与超时（与 config.yaml / PROTOCOL 约定一致的常量；超时可用环境变量覆盖）----
SEARX_PORT="${SEARX_PORT:-8080}"
SEARX_READY_URL="http://127.0.0.1:${SEARX_PORT}/"
SEARX_TIMEOUT="${SEARX_TIMEOUT:-60}"
SEARX_LEGACY_CONTAINER="${SEARX_LEGACY_CONTAINER:-searxng}"
# SearXNG 源码 pin（滚动发布无版本号，钉到实测通过的提交）。**单一真相**：
# devctl setup 按它克隆/切换，scripts/build-runtime.sh 按它校验随包版本。
SEARX_COMMIT="${SEARX_COMMIT:-b512eaa272c0ad6e6c2cca7c6f67dc1feb0ce1bd}"
BACKEND_PORT="${BACKEND_PORT:-14159}"
BACKEND_TIMEOUT="${BACKEND_TIMEOUT:-600}"    # 首次可能触发模型/分词器下载
RAG_PORT="${RAG_PORT:-14160}"
RAG_TIMEOUT="${RAG_TIMEOUT:-600}"            # 嵌入/重排模型加载可能较慢
INFER_PORT="${INFER_PORT:-14161}"
INFER_TIMEOUT="${INFER_TIMEOUT:-600}"        # 首次可能触发模型下载 + 权重加载

# ---- 探活地址：取 config.yaml 里的**绑定地址**，不能写死 127.0.0.1 ----
# profile 部署下服务可能绑 Tailscale 接口（§7 预留 2），此时回环探活打不通、会误判
# 未就绪。绑 0.0.0.0/::（全网卡）时回环可达，探活仍走 127.0.0.1。
probe_host() {  # $1: 配置里的绑定地址 → 可探活的地址
    case "$1" in
        ""|"0.0.0.0"|"::"|"*") echo "127.0.0.1" ;;
        *) echo "$1" ;;
    esac
}

_read_cfg_hosts() {
    local py="$EDGE_BACKEND_PY"
    # 兜底：backend 解释器未就绪（devctl setup 之前）时借 rag 的（仅仓库布局存在）
    [ -x "$py" ] || py="${RAG_DIR:-}/.venv/bin/python"
    [ -x "$py" ] || return 1
    "$py" - "$EDGE_CONFIG" "${1:-hosts}" <<'PY' 2>/dev/null
import json, os, sys, yaml
c = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
inf = c.get("inference") or {}
rem = inf.get("remote") or {}
rag = c.get("rag") or {}
ragc = rag.get("compute") or {}
# env 覆盖=运行时改拓扑不必重打包/改 config：非空即赢（第2行 INFER_HOST、第5行 RAGC_HOST）。
# host 缺省为空即空（不回落 127.0.0.1）——空由 require_host 在 mode=remote 分支拦下。
infer_host = os.environ.get("INFER_REMOTE_HOST", "").strip() or (rem.get("host") or "").strip()
ragc_host = os.environ.get("RAG_COMPUTE_HOST", "").strip() or (ragc.get("host") or "").strip()
if sys.argv[2] == "configuration":
    # UI projection of this same parse, not readiness/authentication or another
    # startup gate. Never return credentials or the configuration document.
    missing = []
    if inf.get("mode", "inprocess") == "remote" and not infer_host:
        missing.append("inference_host")
    local_rag = rag.get("host", "127.0.0.1") in ("", "127.0.0.1", "localhost", "::1", "0.0.0.0", "::", "*")
    if os.environ.get("AGENT_LAYOUT") == "bundle" and local_rag and not ragc_host:
        missing.append("rag_compute_host")
    print(json.dumps({"status": "incomplete" if missing else "configured",
                      "missing": missing, "rag_compute_remote": bool(ragc_host)}))
    sys.exit(0)
print(inf.get("host", "127.0.0.1"))
print(infer_host)
print(rag.get("host", "127.0.0.1"))
print(inf.get("mode", "inprocess"))
print(ragc_host)
print(ragc.get("port") or 14162)
PY
}

edge_configuration() {
    # Parsing/interpreter failures are unknown, never a configured fallback.
    _read_cfg_hosts configuration || printf '%s\n' '{"status":"unknown","missing":[]}'
}

edge_resolve_urls() {
    # 三个 host 一次读出（每次 python 启动约 0.1s，不值得多跑几趟）。
    # venv/解释器缺席时静默回落回环——setup 之前的形态。
    local lines
    lines="$(_read_cfg_hosts)" || lines=""
    if [ -n "$lines" ]; then
        BACKEND_HOST="$(printf '%s\n' "$lines" | sed -n 1p)"
        INFER_HOST="$(printf '%s\n' "$lines" | sed -n 2p)"
        RAG_HOST="$(printf '%s\n' "$lines" | sed -n 3p)"
        INFER_MODE="$(printf '%s\n' "$lines" | sed -n 4p)"
        RAGC_HOST="$(printf '%s\n' "$lines" | sed -n 5p)"
        RAGC_PORT="$(printf '%s\n' "$lines" | sed -n 6p)"
    else
        BACKEND_HOST="127.0.0.1"; INFER_HOST="127.0.0.1"; RAG_HOST="127.0.0.1"; INFER_MODE="inprocess"
        RAGC_HOST=""; RAGC_PORT=14162
    fi
    RAGC_PORT="${RAGC_PORT:-14162}"
    RAG_READY_URL="http://$(probe_host "$RAG_HOST"):${RAG_PORT}/health"
    # RAG 计算面（B2c 拆分；RAGC_HOST 空 = 融合模式，无独立计算服务）
    RAGC_READY_URL="http://$(probe_host "$RAGC_HOST"):${RAGC_PORT}/health"
    BACKEND_READY_URL="http://$(probe_host "$BACKEND_HOST"):${BACKEND_PORT}/health"
    INFER_READY_URL="http://$(probe_host "$INFER_HOST"):${INFER_PORT}/health"
    # 鉴权探针（带钥匙的最便宜 GET）：/health 全是免鉴权的，探不出钥匙错——
    # 钥匙不对时 backend 照样起来、第一句话才炸（实机查验第二轮实证）。
    RAG_AUTH_URL="http://$(probe_host "$RAG_HOST"):${RAG_PORT}/api/v1/category"
    INFER_AUTH_URL="http://$(probe_host "$INFER_HOST"):${INFER_PORT}/metrics"
}

# ---- 通用机制 ----
listen_pids() {  # $1: port
    lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null
}

is_ready() {  # $1: url
    curl -sf -m 3 -o /dev/null "$1"
}

# 进程身份只读 UID / 父 PID / 内核创建时刻，绝不读取 args 或环境。
# Darwin 的 libproc 提供微秒创建时间；Linux 用 boot_id + starttime tick。
# 不用 ps lstart（秒精度不足以排除快速 PID 重用）。不支持的平台安全拒绝。
_edge_process_query() {  # identity|tree, pid, expected identity（tree）
    local py="${EDGE_BACKEND_PY:-}"
    [ -x "$py" ] || py="$(command -v python3)"
    [ -n "$py" ] || return 1
    "$py" - "$@" <<'PY'
import ctypes, errno, os, pathlib, subprocess, sys

mode, raw_pid = sys.argv[1:3]
if not raw_pid.isascii() or not raw_pid.isdecimal() or int(raw_pid) < 2:
    sys.exit(1)
root = int(raw_pid)
if sys.platform == 'darwin':
    # SDK sys/proc_info.h: proc_bsdinfo / PROC_PIDTBSDINFO (3).
    class BSDInfo(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint32) for n in (
            'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid',
            'ruid', 'rgid', 'svuid', 'svgid', 'reserved')]
        _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
        _fields_ += [(n, ctypes.c_uint32) for n in (
            'nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')]
        _fields_ += [('nice', ctypes.c_int32), ('sec', ctypes.c_uint64),
                    ('usec', ctypes.c_uint64)]
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                 ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    def read(pid):
        b = BSDInfo()
        if lib.proc_pidinfo(pid, 3, 0, ctypes.byref(b), ctypes.sizeof(b)) != ctypes.sizeof(b):
            raise OSError(ctypes.get_errno(), 'process identity unavailable')
        if b.status == 5:
            raise ProcessLookupError(errno.ESRCH, 'zombie')
        if b.pid != pid or b.uid != os.geteuid():
            raise ValueError('not an owned live process')
        return b.ppid, f'v1:{pid}:{b.uid}:darwin:{b.sec}:{b.usec}'
    def pids():
        # No command lines: only numeric process IDs.
        return [int(x) for x in subprocess.check_output(
            ['ps', '-axo', 'pid='], text=True).split()]
elif sys.platform.startswith('linux'):
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    def read(pid):
        base = pathlib.Path('/proc') / str(pid)
        uid = base.stat().st_uid
        fields = (base / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            raise ProcessLookupError(errno.ESRCH, 'zombie')
        if uid != os.geteuid():
            raise ValueError('not an owned live process')
        return int(fields[1]), f'v1:{pid}:{uid}:linux:{boot}:{fields[19]}'
    def pids():
        return [int(x.name) for x in pathlib.Path('/proc').iterdir() if x.name.isdecimal()]
else:
    sys.exit(1)
try:
    root_info = read(root)
    if mode == 'identity':
        if len(sys.argv) > 3 and root_info[0] != int(sys.argv[3]):
            sys.exit(1)
        print(root_info[1])
    elif mode == 'tree':
        if root_info[1] != sys.argv[3]:
            sys.exit(1)
        records = {root: root_info}
        candidates = {}
        for pid in pids():
            try:
                candidates[pid] = read(pid)
            except (OSError, ValueError):
                pass
        # An explicit, verified parent chain is required; a listener is never evidence.
        while True:
            additions = {pid: info for pid, info in candidates.items()
                         if pid not in records and info[0] in records}
            if not additions:
                break
            records.update(additions)
        # Recheck the whole chain before handing out a snapshot. Any uncertainty refuses.
        if any(read(pid) != info for pid, info in records.items()):
            sys.exit(1)
        for pid, info in records.items():
            print(pid, info[1])
    else:
        sys.exit(1)
except OSError as e:
    sys.exit(3 if e.errno in (errno.ESRCH, errno.ENOENT) else 1)
except (ValueError, IndexError, subprocess.SubprocessError):
    sys.exit(1)
PY
}

edge_process_identity() { _edge_process_query identity "$@"; }
edge_process_snapshot() { _edge_process_query tree "$1" "$2"; }

# 所有记录写入/读取/条件删除共享同一持久锁文件，不能 unlink 锁本身（会换 inode）。
# flock 属于内核文件锁，进程退出自动释放；不依赖 macOS 没有的 flock CLI。
_edge_process_record() {  # read|write|delete, pidfile, pid, identity
    local py="${EDGE_BACKEND_PY:-}"
    [ -x "$py" ] || py="$(command -v python3)"
    [ -n "$py" ] || return 1
    "$py" - "$@" <<'PY_RECORD'
import fcntl, os, pathlib, re, sys, tempfile
mode, name = sys.argv[1:3]
path = pathlib.Path(name)
sidecar = pathlib.Path(name + '.identity')
def valid(pid, identity):
    return (pid.isascii() and pid.isdecimal() and int(pid) > 1 and
            re.fullmatch(r'v1:' + re.escape(pid) + r':\d+:(?:darwin:\d+:\d+|linux:[^:\s]+:\d+)', identity))
def read_pair():
    return path.read_text().strip(), sidecar.read_text().strip()
def replace_file(target, content):
    fd, temporary = tempfile.mkstemp(prefix=target.name + '.', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(content + '\n')
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
try:
    fd = os.open(name + '.lock', os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if mode == 'read':
            pid, identity = read_pair()
            if not valid(pid, identity):
                sys.exit(1)
            print(pid)
            print(identity)
        elif mode == 'write':
            pid, identity = sys.argv[3:5]
            if not valid(pid, identity):
                sys.exit(1)
            replace_file(sidecar, identity)
            replace_file(path, pid)
        elif mode == 'delete':
            expected = tuple(sys.argv[3:5])
            try:
                actual = read_pair()
            except FileNotFoundError:
                actual = None
            # Comparison and both unlinks stay under the same lock as publication.
            if actual == expected:
                os.unlink(path)
                os.unlink(sidecar)
        else:
            sys.exit(1)
except (OSError, ValueError):
    sys.exit(1)
PY_RECORD
}

_edge_owned_snapshot() {  # pidfile → immutable pid/identity lines
    local pidfile="$1" pid expected current record
    if [ "$#" = 3 ]; then
        pid="$2"; expected="$3"
    else
        record="$(_edge_process_record read "$pidfile")" || return 1
        pid="$(printf '%s\n' "$record" | sed -n 1p)"
        expected="$(printf '%s\n' "$record" | sed -n 2p)"
    fi
    current="$(edge_process_identity "$pid")" || return "$?"
    [ "$current" = "$expected" ] || return 1
    # rc3 from a disappearing descendant is not proof that the root died.
    edge_process_snapshot "$pid" "$expected" || return 1
}

# 端口只用来报告占用。所有本地监听者都必须在已验证的本实例父子树中。
managed_or_report() {  # 名称, 端口, pidfile, stop 目标（兼容调用签名）
    local name="$1" port="$2" pidfile="$3" target="$4" owners snapshot p cmd owned
    owners="$(listen_pids "$port")"
    [ -n "$owners" ] || return 0
    snapshot="$(_edge_owned_snapshot "$pidfile")" || snapshot=""
    owned=1
    for p in $owners; do
        printf '%s\n' "$snapshot" | awk -v pid="$p" '$1 == pid { found=1 } END { exit !found }' || owned=0
    done
    [ "$owned" = 1 ] && return 0
    fail "$name 端口 ${port} 被本脚本管不着的进程占用（${pidfile} 缺少可验证的本实例身份或身份已失效）："
    for p in $owners; do
        cmd="$(ps -o comm= -p "$p" 2>/dev/null || echo '?')"
        echo "      pid $p  $cmd" >&2
    done
    warn "  该端口的监听者不是本次启动器可验证的服务；解释器路径不能证明归属。"
    warn "  请确认进程归属后由其原启动器停止；${EDGE_STOP_CMD:-stop} ${target} 不会自动认领外来进程。"
    return 1
}

# 后台拉起常驻进程。
# 注意：不能用 ( ... & ) 子壳包裹 —— macOS 自带 bash 3.2 的子壳会 wait4 等待
# 后台子进程，而子壳又继承 ssh 会话的 stdout 管道，导致远程单条指令
# `ssh <host> 'devctl.sh start'` 在服务存活期间永不返回。
# 在主 shell 直接 nohup + & 则脚本退出时不等待后台任务，进程被 launchd 收养。
# 带钥匙探一个鉴权端点：401/403 = 钥匙缺失或不对，当场失败并说清；
# 其余状态码放行（服务语义错误不是这道门的事）。curl 失败（不可达）也放行——
# 可达性由 is_ready 门负责，这里只裁决鉴权。
probe_auth() {  # $1: 名称  $2: url  $3: key（可空）
    local code
    if [ -n "${3:-}" ]; then
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
                -H "Authorization: Bearer $3" "$2") || return 0
    else
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$2") || return 0
    fi
    if [ "$code" = "401" ] || [ "$code" = "403" ]; then
        fail "$1 鉴权失败（HTTP ${code}，探针 ${2}）——对应 API key 缺失或不对：与设备 A 的 .env.local 同值、不含引号"
        return 1
    fi
    return 0
}

spawn_daemon() {  # $1: 工作目录  $2: 日志  $3: pid 文件  $4...: 命令
    local dir="$1" log="$2" pidfile="$3" previous="$PWD" pid identity
    shift 3
    cd "$dir" || return 1
    nohup "$@" >>"$log" 2>&1 </dev/null &
    pid=$!
    disown 2>/dev/null || true
    cd "$previous" || return 1
    # $! 是本 shell 刚创建的根，exec 不改变创建时间。记录不含命令/密钥。
    # 保留纯 PID 文件给现有 caller；sidecar 缺失绝不能退回只凭 PID 停止。
    identity="$(edge_process_identity "$pid" "$$")" || {
        fail "启动进程 pid $pid 无法记录身份，未取得停止权限，请由原启动器核查"
        return 1
    }
    _edge_process_record write "$pidfile" "$pid" "$identity" || {
        fail "启动进程 pid $pid 身份记录写入失败，不能自动停止"
        return 1
    }
}

# 日志代际隔离：本次启动尝试的起点调用，把上一代进程的日志轮转为 $1.prev（覆盖旧 .prev）。
# 刻意**不放进 spawn_daemon 内部**——backend 的密钥盘点行在 spawn 之前就写入本代日志
# （第一现场证据，必须留在本代）。轮转点由各 start_* 在首次向日志写入/spawn 之前显式调用，
# 免得 devctl 失败时 tail 把上一代的旧拒启信息混进来误导诊断。
rotate_log() {  # $1: 日志路径——非空则轮转为 $1.prev（覆盖旧 .prev）
    [ -s "$1" ] && mv -f "$1" "$1.prev" || true
}

# 地址非空断言：config 的 host 缺省为空（通用模板不钉机器 IP），分离部署必填。
# 空 host 会拼出 `http://:14161` 的晦涩探活失败——提前拦成一条明确指引。
require_host() {  # $1: 值  $2: 名称  $3: 指引 → 值非空返 0；空则 fail 指引返 1
    local v; v="$(printf '%s' "$1" | tr -d '[:space:]')"
    [ -n "$v" ] && return 0
    fail "$2 地址为空——$3"
    return 1
}

# 轮询就绪探针；期间守护进程若已退出则立即失败并展示日志尾部
wait_ready() {  # $1: 名称  $2: url  $3: 超时秒  $4: pid  $5: 日志文件
    local name="$1" url="$2" timeout="$3" pid="$4" log="$5" waited=0
    printf "[edge] 等待 %s 就绪 " "$name"
    while ! is_ready "$url"; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo ""
            fail "$name 进程已退出，最近日志（${log}）："
            tail -n 30 "$log" >&2
            return 1
        fi
        if [ "$waited" -ge "$timeout" ]; then
            echo ""
            fail "$name 在 ${timeout}s 内未就绪，最近日志（${log}）："
            tail -n 30 "$log" >&2
            return 1
        fi
        sleep 2; waited=$((waited + 2)); printf "."
    done
    echo ""
    info "$name 就绪 ✅ ($url)"
}

# 「探活不通但端口有人监听」的岔路（start_* 的第二道前置）。此前这条路直接漏去 spawn：
# 外来占用者若对 /health 回 404（如任意 http 服务占着端口），is_ready 不通、
# managed_or_report 根本不会被调到，于是照常再拉一个进程——而 BSD 的 SO_REUSEADDR
# 语义下「特定地址」与「通配地址」可**同端口共存**（实证：0.0.0.0 上的 http.server
# 占着 14159，backend 绑 127.0.0.1:14159 照样成功、回环流量被它接走），双监听
# 悄悄成立，谁也不报错。故探活不通时还要看端口：
#   本机无监听            = 真没在跑 → 放行去 spawn（返回 0）
#   有监听 + pid 记录存活  = 我们的实例正在启动中 → 等它就绪，不许再拉一份（返回 2）
#   有监听 + 无 pid 记录   = 外来占用者 → managed_or_report 硬失败报告占用者（返回 1）
guard_port_before_spawn() {  # $1名称 $2端口 $3pid文件 $4stop目标 $5就绪url $6超时 $7日志 → 0/1/2
    local name="$1" port="$2" pidfile="$3" target="$4" url="$5" timeout="$6" log="$7"
    [ -n "$(listen_pids "$port")" ] || return 0
    managed_or_report "$name" "$port" "$pidfile" "$target" || return 1
    info "$name 已在启动中（pid 记录存活，探活未通），等待其就绪而非再拉一份"
    wait_ready "$name" "$url" "$timeout" "$(cat "$pidfile")" "$log" || return 1
    return 2
}

stop_one() {  # 名称, 端口, pidfile, 宽限秒
    local name="$1" port="$2" pidfile="$3" grace="$4" snapshot p identity current rc
    local recorded_pid recorded_identity record
    record="$(_edge_process_record read "$pidfile")" || {
        if [ ! -f "$pidfile" ] && [ -z "$(listen_pids "$port")" ]; then
            info "$name 无本实例运行记录"
            return 0
        fi
        fail "$name 拒绝自动停止：缺少完整有效的启动身份记录；未发送信号"
        return 1
    }
    recorded_pid="$(printf '%s\n' "$record" | sed -n 1p)"
    recorded_identity="$(printf '%s\n' "$record" | sed -n 2p)"
    if snapshot="$(_edge_owned_snapshot "$pidfile" "$recorded_pid" "$recorded_identity")"; then
        rc=0
    else
        rc=$?
    fi
    if [ "$rc" != 0 ]; then
        if [ "$rc" = 3 ] && [ -z "$(listen_pids "$port")" ]; then
            _edge_process_record delete "$pidfile" "$recorded_pid" "$recorded_identity" || {
                fail "$name 根进程已退出，但旧记录清理失败"
                return 1
            }
            info "$name 记录的根进程已退出，已清理匹配的旧记录；未认领其他进程，也未断言未知后代已退出"
            return 0
        fi
        fail "$name 拒绝自动停止：PID 已重用、身份不可读或根已退出但仍有未认领监听者；未发送信号"
        return 1
    fi
    [ -n "$snapshot" ] || { fail "$name 进程身份快照为空，拒绝自动停止"; return 1; }
    info "停止 ${name}（已验证本实例父子进程身份）"
    # 每次信号前核对创建身份。父进程退出后只保留之前验证过的子进程快照。
    while read -r p identity; do
        current="$(edge_process_identity "$p")" || continue
        [ "$current" = "$identity" ] && kill -TERM "$p" 2>/dev/null
    done <<< "$snapshot"
    # 0.2 s 粒度轮询、宽限按墙钟截止（grace 仍以秒计）：服务通常在 TERM 后几十毫秒内退出，
    # 1 s 粒度让每个服务白等近 1 s，三个服务串行就是 ~3 s 的 ⌘Q 卡顿（2026-10-29）。
    # 不按 tick 计数：每 tick 对每个 pid 起一次 python 核身份，计数会把宽限悄悄拉长。
    local started=$SECONDS alive
    while [ $((SECONDS - started)) -lt "$grace" ]; do
        alive=0
        while read -r p identity; do
            current="$(edge_process_identity "$p")" || continue
            [ "$current" = "$identity" ] && alive=1
        done <<< "$snapshot"
        [ "$alive" = 0 ] && break
        sleep 0.2
    done
    while read -r p identity; do
        current="$(edge_process_identity "$p")" || continue
        if [ "$current" = "$identity" ]; then
            warn "$name pid $p 未响应 TERM，强制结束已验证的同一进程"
            kill -KILL "$p" 2>/dev/null || {
                fail "$name pid $p 发送 KILL 失败，保留身份记录"
                return 1
            }
        fi
    done <<< "$snapshot"
    # KILL 成功只表示信号已发送，退出仍须观察；身份读取失败不能冒充退出。
    started=$SECONDS
    while :; do
        alive=0
        while read -r p identity; do
            if current="$(edge_process_identity "$p")"; then rc=0; else rc=$?; fi
            if [ "$rc" = 3 ]; then continue; fi
            if [ "$rc" != 0 ]; then
                fail "$name pid $p 无法确认退出，保留身份记录"
                return 1
            fi
            [ "$current" = "$identity" ] && alive=1
        done <<< "$snapshot"
        [ "$alive" = 0 ] && break
        if [ $((SECONDS - started)) -ge 5 ]; then
            fail "$name 已发送停止信号，但本实例仍存活，保留身份记录"
            return 1
        fi
        sleep 0.2
    done
    # 与 spawn 的发布共享锁，CAS 比较到删除之间不能插入新代写入。
    _edge_process_record delete "$pidfile" "$recorded_pid" "$recorded_identity" || {
        fail "$name 进程已退出，但身份记录清理失败"
        return 1
    }
    info "$name 本实例进程已停止（其他端口占用者不受影响）"
}

status_one() {  # $1: 名称  $2: 端口  $3: 就绪 url
    local pids; pids="$(listen_pids "$2" | tr '\n' ' ')"
    if [ -n "$pids" ]; then
        if is_ready "$3"; then
            echo -e "  ${GREEN:-}●${NC:-} $1\tport $2\tpid $pids\t就绪"
        else
            echo -e "  ${YELLOW:-}●${NC:-} $1\tport $2\tpid $pids\t启动中/未就绪"
        fi
    elif is_ready "$3"; then
        # 本机该端口没进程、但探活通 = 服务在远端（profile 部署：设备 A 的 RAG/推理）。
        # 不能报"未运行"——那会让人在设备 B 上白折腾一个其实健康的远端服务。
        echo -e "  ${GREEN:-}●${NC:-} $1\t远端\t\t就绪 ($3)"
    else
        echo -e "  ${RED:-}●${NC:-} $1\tport $2\t未运行"
    fi
}

status_searx() {  # 与 status_one 同构，仅"未运行"标注（可选）
    local pids; pids="$(listen_pids "$SEARX_PORT" | tr '\n' ' ')"
    if [ -n "$pids" ]; then
        if is_ready "$SEARX_READY_URL"; then
            echo -e "  ${GREEN:-}●${NC:-} SearXNG \tport $SEARX_PORT\tpid $pids\t就绪"
        else
            echo -e "  ${YELLOW:-}●${NC:-} SearXNG \tport $SEARX_PORT\tpid $pids\t启动中/未就绪"
        fi
    else
        echo -e "  ${RED:-}●${NC:-} SearXNG \tport $SEARX_PORT\t未运行（可选）"
    fi
}

# ---- SearXNG ----
# 确保配置存在且开启 json 输出（web_research 靠 json 拿结构化结果）。
# 配置写到 EDGE_SEARX_SETTINGS（bundle 布局下它在数据根——bundle 本体只读）。
ensure_searx_settings() {
    local sf="$EDGE_SEARX_SETTINGS"
    mkdir -p "$(dirname "$sf")"
    if [ -f "$sf" ]; then
        # 旧模板自愈（幂等）：标量 use_default_settings → 映射形式，从默认引擎表剔除
        # 需 Tor 代理的暗网引擎（本机无 Tor，ahmia/torch 每次启动注册失败刷 ERROR）。
        if grep -qE '^use_default_settings:[[:space:]]*true[[:space:]]*$' "$sf"; then
            /usr/bin/perl -i -pe 's/^use_default_settings:\s*true\s*$/use_default_settings:\n  engines:\n    remove:          # 暗网引擎需 Tor 代理，本机注册必败；本项目不路由 onion\n      - ahmia\n      - torch/' "$sf"
            info "SearXNG 配置自愈：已从默认引擎表剔除 ahmia/torch（无 Tor 代理，注册必败刷 ERROR）"
        fi
        grep -qE '^[[:space:]]*port:' "$sf" \
            || warn "已有 $sf 未声明 server.port——原生 SearXNG 默认监听 8888，请在 server: 下加入 port: ${SEARX_PORT}"
        if grep -qE '^[[:space:]]*-[[:space:]]*json([[:space:]]|$)' "$sf" || grep -qE 'formats:.*json' "$sf"; then
            return 0
        fi
        warn "已有 $sf 但未开启 json 输出；web_research 需要它，请在 search.formats 下加入 - json"
        return 0
    fi
    # 首次生成最小覆盖配置：use_default_settings 只覆写必要项，其余继承 SearXNG 默认
    local secret
    secret="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' 2>/dev/null)"
    [ -n "$secret" ] || secret="devsecret-$(date +%s)"
    cat > "$sf" <<EOF
# 由 lib_edge.sh 生成：仅覆写必要项，其余继承 SearXNG 默认设置
use_default_settings:
  engines:
    remove:          # 暗网引擎需 Tor 代理，本机注册必败（启动刷 ERROR）；本项目不路由 onion
      - ahmia
      - torch
server:
  secret_key: "$secret"
  bind_address: "127.0.0.1"  # 仅本机回环：limiter 已关，绑 0.0.0.0 会让局域网白嫖搜索代理
  port: $SEARX_PORT           # 原生 webapp 默认 8888，必须显式指到 config.yaml 约定的端口
  limiter: false          # 本机自用可关；对外暴露务必开启
search:
  formats:
    - html
    - json                # web_research 靠 json 输出拿结构化结果，必须开启
EOF
    info "已生成 SearXNG 配置: ${sf}（已开启 html + json 输出）"
}

# 旧 Docker 部署只有名字，没有本实例启动凭据。名字相同不等于归属相同，
# 不再隐式 rm -f；由原启动器处理迁移。Docker 状态不代表原生服务的归属或可用性。
remove_legacy_searx_container() {  # 保留旧函数名供 caller 兼容，现仅检测/报告
    command -v docker >/dev/null 2>&1 || return 0
    local containers
    containers="$(docker ps -aq -f "name=^${SEARX_LEGACY_CONTAINER}$" 2>/dev/null)" || {
        warn "无法核查旧 SearXNG Docker 容器状态，未操作容器；原生服务继续按自己的端口和进程身份检查"
        return 0
    }
    if [ -n "$containers" ]; then
        warn "发现同名 SearXNG Docker 容器，名字无法证明本实例归属，未自动删除；如占用端口请由原启动器处理，原生服务独立检查"
    fi
    return 0
}

start_searx() {
    remove_legacy_searx_container || return 1
    if is_ready "$SEARX_READY_URL"; then
        managed_or_report "SearXNG" "$SEARX_PORT" "$EDGE_LOG_DIR/searx.pid" searx || return 1
        info "SearXNG 已在运行，跳过"; return 0
    fi
    guard_port_before_spawn "SearXNG" "$SEARX_PORT" "$EDGE_LOG_DIR/searx.pid" searx \
        "$SEARX_READY_URL" "$SEARX_TIMEOUT" "$EDGE_LOG_DIR/searx.log"
    case $? in 1) return 1 ;; 2) return 0 ;; esac
    [ -x "$EDGE_SEARX_PY" ] || { warn "缺少 SearXNG 解释器（${EDGE_SEARX_PY}，可选层），跳过 SearXNG${EDGE_SETUP_HINT:+；如需联网搜索$EDGE_SETUP_HINT}"; return 1; }
    ensure_searx_settings
    info "启动 SearXNG (port $SEARX_PORT) → $EDGE_LOG_DIR/searx.log"
    rotate_log "$EDGE_LOG_DIR/searx.log"   # 本代日志起点：隔绝上一代进程日志混入诊断
    # bind_address/port 由 settings.yml 决定（127.0.0.1:8080，仅本机回环）
    spawn_daemon "$EDGE_SEARX_DIR" "$EDGE_LOG_DIR/searx.log" "$EDGE_LOG_DIR/searx.pid" \
        env SEARXNG_SETTINGS_PATH="$EDGE_SEARX_SETTINGS" "$EDGE_SEARX_PY" -m searx.webapp || return 1
    wait_ready "SearXNG" "$SEARX_READY_URL" "$SEARX_TIMEOUT" "$(cat "$EDGE_LOG_DIR/searx.pid")" "$EDGE_LOG_DIR/searx.log"
}

stop_searx() {
    # 只报告旧容器；stop_one 不认领 Docker 的端口代理。
    local failed=0
    remove_legacy_searx_container || failed=1
    stop_one "SearXNG" "$SEARX_PORT" "$EDGE_LOG_DIR/searx.pid" 5 || failed=1
    return "$failed"
}

# ---- RAG 数据面（B2c 拆分：rag.host 为本机时归 edge 编队）----
# 编队顺序见 edge_up：单机（inprocess）形态先于 backend 就绪；remote 形态与 backend
# 同时拉起（backend 就绪失败时撤掉本次拉起的 RAG）。
# 判据：rag.host 指本机（回环/通配/空）= 数据面是本机编队成员；指远端 = 只探活不管
#（拆分切换前的过渡拓扑：整个 RAG 还在算力端，本判据让 edge 行为与切换前逐字一致）。
rag_is_local() {
    case "$RAG_HOST" in
        ""|"127.0.0.1"|"localhost"|"::1"|"0.0.0.0"|"::"|"*") return 0 ;;
        *) return 1 ;;
    esac
}

# 拉起与等就绪可拆开（远端编队并行起 backend 与 RAG）：start_* 带 --no-wait 时只过前置门
# 并拉起，真拉起了才记一笔待等，由 wait_*_spawned 补等；不带参数时行为与 CLI 一致不变。
# 「已在运行」「本实例启动中（守门已等到就绪）」两条路本来就不留待等。
start_rag_local() {  # [--no-wait]
    local nowait=0
    [ "${1:-}" = "--no-wait" ] && nowait=1
    _EDGE_PENDING_RAG=0
    if is_ready "$RAG_READY_URL"; then
        managed_or_report "RAG" "$RAG_PORT" "$EDGE_LOG_DIR/rag.pid" rag || return 1
        info "RAG 已在运行，跳过"; return 0
    fi
    guard_port_before_spawn "RAG" "$RAG_PORT" "$EDGE_LOG_DIR/rag.pid" rag \
        "$RAG_READY_URL" "$RAG_TIMEOUT" "$EDGE_LOG_DIR/rag.log"
    case $? in 1) return 1 ;; 2) return 0 ;; esac
    [ -n "${EDGE_RAG_PY:-}" ] || { fail "本布局未注入 RAG 承载（EDGE_RAG_PY）而 rag.host 指向本机"; return 1; }
    [ -x "$EDGE_RAG_PY" ] || { fail "缺少 RAG 解释器（${EDGE_RAG_PY}）${EDGE_SETUP_HINT:+，$EDGE_SETUP_HINT}"; return 1; }
    info "启动 RAG (port $RAG_PORT) → $EDGE_LOG_DIR/rag.log"
    rotate_log "$EDGE_LOG_DIR/rag.log"   # 本代日志起点：隔绝上一代进程日志混入诊断
    # RAG_CONFIG/RAG_DATA_DIR 布局注入：bundle 的代码目录只读，数据必须落数据根；
    # 仓库布局 EDGE_RAG_DATA_DIR 为空 → rag_server 缺省用 rag/ 本目录（现状）
    spawn_daemon "$EDGE_RAG_DIR" "$EDGE_LOG_DIR/rag.log" "$EDGE_LOG_DIR/rag.pid" \
        env RAG_CONFIG="$EDGE_CONFIG" RAG_DATA_DIR="${EDGE_RAG_DATA_DIR:-}" \
        "$EDGE_RAG_PY" rag_server.py || return 1
    if [ "$nowait" = 1 ]; then _EDGE_PENDING_RAG=1; return 0; fi
    wait_ready "RAG" "$RAG_READY_URL" "$RAG_TIMEOUT" "$(cat "$EDGE_LOG_DIR/rag.pid")" "$EDGE_LOG_DIR/rag.log"
}

wait_rag_spawned() {  # 补等 start_rag_local --no-wait 拉起的进程；未拉起则无事可等
    [ "${_EDGE_PENDING_RAG:-0}" = 1 ] || return 0
    _EDGE_PENDING_RAG=0
    wait_ready "RAG" "$RAG_READY_URL" "$RAG_TIMEOUT" "$(cat "$EDGE_LOG_DIR/rag.pid")" "$EDGE_LOG_DIR/rag.log"
}

stop_rag_local() { stop_one "RAG" "$RAG_PORT" "$EDGE_LOG_DIR/rag.pid" 15; }

# ---- backend ----
start_backend() {  # [--no-wait]（见 start_rag_local 上方说明）
    local nowait=0
    [ "${1:-}" = "--no-wait" ] && nowait=1
    _EDGE_PENDING_BACKEND=0
    if is_ready "$BACKEND_READY_URL"; then
        managed_or_report "backend" "$BACKEND_PORT" "$EDGE_LOG_DIR/backend.pid" backend || return 1
        info "backend 已在运行，跳过"; return 0
    fi
    guard_port_before_spawn "backend" "$BACKEND_PORT" "$EDGE_LOG_DIR/backend.pid" backend \
        "$BACKEND_READY_URL" "$BACKEND_TIMEOUT" "$EDGE_LOG_DIR/backend.log"
    case $? in 1) return 1 ;; 2) return 0 ;; esac
    [ -x "$EDGE_BACKEND_PY" ] || { fail "缺少 backend 解释器（${EDGE_BACKEND_PY}）${EDGE_SETUP_HINT:+，$EDGE_SETUP_HINT}"; return 1; }
    # 本代日志起点：轮转旧 backend.log 为 .prev，隔绝上一代进程的旧拒启信息混进诊断。
    # 紧随其后的密钥盘点行即写入本代 backend.log（属于本代，是本次 spawn 的第一现场证据）。
    rotate_log "$EDGE_LOG_DIR/backend.log"
    # 密钥盘点（只记有无+长度，不记内容）：401 类故障的第一现场证据。必须在
    # 一切门（可达性/鉴权）**之前**记——门一拦，事后就没有「注入了什么」的证据
    #（实机查验第三轮实证：鉴权门拦停后 backend.log 里空空如也）。
    key_stat() { if [ -n "$1" ]; then echo "有(${#1}字)"; else echo "无"; fi; }
    echo "[edge] $(date '+%H:%M:%S') 密钥盘点: AGENT=$(key_stat "${AGENT_API_KEY:-}") INFER=$(key_stat "${INFER_API_KEY:-}") RAG=$(key_stat "${RAG_API_KEY:-}")" \
        >>"$EDGE_LOG_DIR/backend.log"
    # 本地历史/文件必须独立于算力端可读。模型下载、身份/鉴权校验及依赖
    # 就绪由 backend 的单一后台装配任务负责；未就绪时执行接口返回 503。
    # 不在 HTTP 数据服务之前等待 RAG、远端推理或 tokenizer 下载。
    if [ "$INFER_MODE" != "remote" ]; then
        # Direct `devctl start backend` also retains the in-process contract.
        if ! is_ready "$RAG_READY_URL"; then
            fail "RAG 未就绪，单机执行层等待依赖后启动"
            return 1
        fi
        probe_auth "RAG" "$RAG_AUTH_URL" "${RAG_API_KEY:-}" || return 1
        info "检查本机模型（未下载时显示下载进度）…"
        ( cd "$EDGE_BACKEND_DIR" && "$EDGE_BACKEND_PY" scripts/fetch_model.py ) \
            || { fail "模型未就绪，中止 backend 启动"; return 1; }
    fi
    info "启动 backend (port $BACKEND_PORT) → $EDGE_LOG_DIR/backend.log"
    spawn_daemon "$EDGE_BACKEND_DIR" "$EDGE_LOG_DIR/backend.log" "$EDGE_LOG_DIR/backend.pid" \
        "$EDGE_BACKEND_PY" app.py || return 1
    if [ "$nowait" = 1 ]; then _EDGE_PENDING_BACKEND=1; return 0; fi
    wait_ready "backend" "$BACKEND_READY_URL" "$BACKEND_TIMEOUT" "$(cat "$EDGE_LOG_DIR/backend.pid")" "$EDGE_LOG_DIR/backend.log"
}

wait_backend_spawned() {  # 补等 start_backend --no-wait 拉起的进程；未拉起则无事可等
    [ "${_EDGE_PENDING_BACKEND:-0}" = 1 ] || return 0
    _EDGE_PENDING_BACKEND=0
    wait_ready "backend" "$BACKEND_READY_URL" "$BACKEND_TIMEOUT" "$(cat "$EDGE_LOG_DIR/backend.pid")" "$EDGE_LOG_DIR/backend.log"
}

stop_backend() { stop_one "backend" "$BACKEND_PORT" "$EDGE_LOG_DIR/backend.pid" 20; }

# ---- edge 编队（app 与 devctl edge profile 共用的顺序与容错）----
# 数据服务优先；计算与工具注册在后端后台装配，晚到依赖沿既有重扫机制补齐。
edge_up() {
    edge_require_env || return 1
    mkdir -p "$EDGE_LOG_DIR"
    if [ "$INFER_MODE" != "remote" ]; then
        # In-process assembly retains synchronous model/tool startup. Keep its
        # established dependency ordering so optional tools are not lost.
        if rag_is_local; then
            start_rag_local || return 1
        fi
        start_searx || warn "SearXNG 未就绪，联网搜索暂不可用"
        start_backend
        return $?
    fi
    # 先提供本地历史；依赖服务可以稍后就绪，失败不撤掉数据服务。
    # backend 与本机 RAG 先后拉起、再逐个等就绪：RAG 冷启与 backend 启动/装配重叠，
    # 装配首试就可能见到 RAG（串行时首试必撞 RAG 未起）。backend 拉起或就绪失败仍是
    # 硬失败（拉起失败时 RAG 不拉；就绪失败时撤掉本次拉起的 RAG，守住「没有 backend
    # 就没有 RAG」——事先已在跑的 RAG 不是本次拉起的，不动）；RAG 任一步失败只警告。
    local rag_ok=1
    start_backend --no-wait || return 1
    if rag_is_local; then
        start_rag_local --no-wait || rag_ok=0
    fi
    if ! wait_backend_spawned; then
        if [ "${_EDGE_PENDING_RAG:-0}" = 1 ]; then
            _EDGE_PENDING_RAG=0
            stop_rag_local || warn "撤掉本次拉起的 RAG 失败（${EDGE_LOG_DIR}/rag.pid），请 down 收摊"
        fi
        return 1
    fi
    if rag_is_local; then
        if [ "$rag_ok" = 1 ]; then wait_rag_spawned || rag_ok=0; fi
        [ "$rag_ok" = 1 ] || warn "RAG 未就绪，本地历史可用；执行能力等待依赖恢复"
    fi
    start_searx || warn "SearXNG 未就绪，联网搜索暂不可用"
    return 0
}

edge_down() {
    edge_require_env || return 1
    local failed=0
    stop_backend || failed=1
    stop_searx || failed=1
    if rag_is_local && [ -n "${EDGE_RAG_PY:-}" ]; then
        stop_rag_local || failed=1
    fi
    return "$failed"
}

# ---- 孤儿回收：本数据根中的启动身份记录是唯一认领凭据 ----
# app 崩溃留下的有档实例仍可停止。无档监听者即使使用相同 bundle 解释器也
# 不足以证明属于本实例；只报告，不能读取 args 或按宽泛 .app 路径认领。
edge_reap() {
    edge_require_env || return 1
    local svc port pidfile failed=0
    for svc in backend searx rag; do
        case "$svc" in
            backend) port="$BACKEND_PORT" ;;
            searx) port="$SEARX_PORT" ;;
            rag) port="$RAG_PORT"; [ -n "${EDGE_RAG_PY:-}" ] || continue ;;
        esac
        pidfile="$EDGE_LOG_DIR/$svc.pid"
        if [ -f "$pidfile" ]; then
            info "核查上代 ${svc} 的启动身份记录"
            stop_one "$svc" "$port" "$pidfile" 20 || failed=1
        elif [ -n "$(listen_pids "$port")" ]; then
            warn "${svc} 有无档监听者，无法证明属于本实例，拒绝自动回收"
            failed=1
        fi
    done
    return "$failed"
}

edge_status() {
    edge_require_env || return 1
    echo "[edge] 服务状态（inference.mode=${INFER_MODE}）："
    status_one "RAG     " "$RAG_PORT" "$RAG_READY_URL"
    # B2c 拆分模式（rag.compute.host 非空）才有独立计算面；融合模式无此服务
    [ -n "${RAGC_HOST:-}" ] && status_one "RAG计算 " "$RAGC_PORT" "$RAGC_READY_URL"
    status_searx
    if [ "$INFER_MODE" = "remote" ]; then
        status_one "推理服务" "$INFER_PORT" "$INFER_READY_URL"
    fi
    status_one "backend " "$BACKEND_PORT" "$BACKEND_READY_URL"
    echo "[edge] 日志目录: $EDGE_LOG_DIR"
}

# ---- 就绪 URL 装配（source 时立即执行；直接执行模式在 dispatch 前也会跑到这里）----
edge_require_env || { [ "${BASH_SOURCE[0]}" != "$0" ] && return 1 || exit 1; }
edge_resolve_urls

# ---- 直接执行模式（SidecarManager 用）：`lib_edge.sh up|down|status` ----
# source 进来（devctl）不走这里——BASH_SOURCE 与 $0 不同即为被 source。
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    set -u
    case "${1:-}" in
        up)     edge_up ;;
        down)   edge_down ;;
        reap)   edge_reap ;;
        status) edge_status ;;
        configuration) edge_configuration ;;
        *)
            echo "用法: EDGE_*=... $0 up|down|reap|status|configuration（布局参数见文件头）" >&2
            exit 1
            ;;
    esac
fi
