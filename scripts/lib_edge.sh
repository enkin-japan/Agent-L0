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
    "$py" - "$EDGE_CONFIG" <<'PY' 2>/dev/null
import os, sys, yaml
c = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
inf = c.get("inference") or {}
rem = inf.get("remote") or {}
rag = c.get("rag") or {}
ragc = rag.get("compute") or {}
# env 覆盖=运行时改拓扑不必重打包/改 config：非空即赢（第2行 INFER_HOST、第5行 RAGC_HOST）
infer_host = os.environ.get("INFER_REMOTE_HOST", "").strip() or rem.get("host", "127.0.0.1")
ragc_host = os.environ.get("RAG_COMPUTE_HOST", "").strip() or (ragc.get("host") or "").strip()
print(inf.get("host", "127.0.0.1"))
print(infer_host)
print(rag.get("host", "127.0.0.1"))
print(inf.get("mode", "inprocess"))
print(ragc_host)
print(ragc.get("port") or 14162)
PY
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

# 「已在运行，跳过」的前置判定。探活只能证明端口上有人应答，证不了那是本脚本起的
# 服务——三种情形必须分开：
#   本机无监听进程 + 探活通       = 服务在远端（profile 部署：设备 A 的 RAG/推理），正常跳过；
#   本机有监听进程 + pid 记录存活 = 确是本脚本起的，正常跳过；
#   本机有监听进程 + 无 pid 记录  = 端口被本脚本管不着的进程占着（手工起的 / 残留 / 别的程序）。
# 第三种此前与第二种一样被静默"跳过"：用户看到"已在运行 ✅"，实际既没起它、
# 也不认识它，故障推迟到访问时才以连不上的形式暴露（B3 实证：设备 B 报"backend 已在
# 运行，跳过"，而 logs/ 里连 backend.log 都没有——真正该启动的服务被静默略过）。
# 故第三种改为硬失败并报告占用者，让用户定夺——静默略过比报错难查得多。
# 注意判据是"pid 记录仍存活"而非"记录 pid == 监听 pid"：前端的 pid 记的是 npm，
# 真正监听 5173 的是它派生的 vite（stop_one 同样按端口兜底回收子进程）。
managed_or_report() {  # $1: 名称  $2: 端口  $3: pid 文件  $4: stop 目标名 → 0=可跳过 1=被外部占用
    local name="$1" port="$2" pidfile="$3" target="$4" owners recorded
    owners="$(listen_pids "$port")"
    [ -n "$owners" ] || return 0            # 本机没进程：服务在远端，不归本机管
    if [ -f "$pidfile" ]; then
        recorded="$(cat "$pidfile" 2>/dev/null)"
        [ -n "$recorded" ] && kill -0 "$recorded" 2>/dev/null && return 0
    fi
    fail "$name 端口 ${port} 被本脚本管不着的进程占用（${pidfile} 无记录或记录已失效）："
    for p in $owners; do
        echo "      pid $p  $(ps -o comm= -p "$p" 2>/dev/null || echo '?')" >&2
    done
    # ${} 必须显式界定：紧跟其后的全角句号是多字节字符，$EDGE_LOG_DIR。 会被 bash 连着
    # 首字节当成变量名（set -u 下直接 unbound variable 报错）。
    # 措辞对两条进入路径都成立：探活通（像我们的服务但没记录）与探活不通（占用者
    # 对 /health 回 404/拒答）都会走到这里
    warn "  该端口的监听者不是本次启动器拉起的服务，其日志也不在 ${EDGE_LOG_DIR}。"
    warn "  确认可回收则先停： ${EDGE_STOP_CMD:-stop} ${target}"
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
    local dir="$1" log="$2" pidfile="$3"
    shift 3
    cd "$dir" || return 1
    nohup "$@" >>"$log" 2>&1 </dev/null &
    echo $! >"$pidfile"
    disown 2>/dev/null || true
    cd "$OLDPWD" || return 1
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

stop_one() {  # $1: 名称  $2: 端口  $3: pid 文件  $4: 宽限秒（先 TERM 后 KILL）
    local name="$1" port="$2" pidfile="$3" grace="$4"
    local pids=""
    [ -f "$pidfile" ] && pids="$(cat "$pidfile" 2>/dev/null)"
    pids="$(printf '%s\n%s' "$pids" "$(listen_pids "$port")" | sort -u | grep -E '^[0-9]+$' || true)"
    if [ -z "$pids" ]; then
        info "$name 未在运行"
        rm -f "$pidfile"
        return 0
    fi
    info "停止 $name (pid: $(echo $pids | tr '\n' ' '))"
    # 先 TERM 给 FastAPI lifespan 留出收尾时间（如 backend 冻结会话落盘）
    echo "$pids" | xargs kill 2>/dev/null
    local waited=0
    while [ "$waited" -lt "$grace" ]; do
        local alive=""
        for p in $pids; do kill -0 "$p" 2>/dev/null && alive="1"; done
        [ -z "$alive" ] && [ -z "$(listen_pids "$port")" ] && break
        sleep 1; waited=$((waited + 1))
    done
    # 宽限期后仍存活则强杀（含端口上的残留子进程，如 npm 下的 vite）
    local rest
    rest="$(printf '%s\n%s' "$pids" "$(listen_pids "$port")" | sort -u | grep -E '^[0-9]+$' || true)"
    for p in $rest; do kill -0 "$p" 2>/dev/null && { warn "$name pid $p 未响应 TERM，强制结束"; kill -9 "$p" 2>/dev/null; }; done
    rm -f "$pidfile"
    info "$name 已停止"
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

# 迁移期兼容：移除旧 docker 部署的 SearXNG 容器（带 --restart unless-stopped，
# 不显式删除会一直复活并占着 8080）。docker 未装/未运行则静默跳过。
remove_legacy_searx_container() {
    command -v docker >/dev/null 2>&1 || return 0
    if [ -n "$(docker ps -aq -f "name=^${SEARX_LEGACY_CONTAINER}$" 2>/dev/null)" ]; then
        info "发现旧的 SearXNG docker 容器，移除（已迁移为原生部署）"
        docker rm -f "$SEARX_LEGACY_CONTAINER" >/dev/null 2>&1 || true
        sleep 1   # 等 docker 的端口代理释放 8080
    fi
}

start_searx() {
    remove_legacy_searx_container
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
    # bind_address/port 由 settings.yml 决定（127.0.0.1:8080，仅本机回环）
    spawn_daemon "$EDGE_SEARX_DIR" "$EDGE_LOG_DIR/searx.log" "$EDGE_LOG_DIR/searx.pid" \
        env SEARXNG_SETTINGS_PATH="$EDGE_SEARX_SETTINGS" "$EDGE_SEARX_PY" -m searx.webapp || return 1
    wait_ready "SearXNG" "$SEARX_READY_URL" "$SEARX_TIMEOUT" "$(cat "$EDGE_LOG_DIR/searx.pid")" "$EDGE_LOG_DIR/searx.log"
}

stop_searx() {
    # 先清旧 docker 容器再走端口 pid——否则 lsof 命中的是 docker 的端口代理进程，误杀会连累 Docker Desktop
    remove_legacy_searx_container
    stop_one "SearXNG" "$SEARX_PORT" "$EDGE_LOG_DIR/searx.pid" 5
}

# ---- RAG 数据面（B2c 拆分：rag.host 为本机时归 edge 编队，先于 backend）----
# 判据：rag.host 指本机（回环/通配/空）= 数据面是本机编队成员；指远端 = 只探活不管
#（拆分切换前的过渡拓扑：整个 RAG 还在算力端，本判据让 edge 行为与切换前逐字一致）。
rag_is_local() {
    case "$RAG_HOST" in
        ""|"127.0.0.1"|"localhost"|"::1"|"0.0.0.0"|"::"|"*") return 0 ;;
        *) return 1 ;;
    esac
}

start_rag_local() {
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
    # RAG_CONFIG/RAG_DATA_DIR 布局注入：bundle 的代码目录只读，数据必须落数据根；
    # 仓库布局 EDGE_RAG_DATA_DIR 为空 → rag_server 缺省用 rag/ 本目录（现状）
    spawn_daemon "$EDGE_RAG_DIR" "$EDGE_LOG_DIR/rag.log" "$EDGE_LOG_DIR/rag.pid" \
        env RAG_CONFIG="$EDGE_CONFIG" RAG_DATA_DIR="${EDGE_RAG_DATA_DIR:-}" \
        "$EDGE_RAG_PY" rag_server.py || return 1
    wait_ready "RAG" "$RAG_READY_URL" "$RAG_TIMEOUT" "$(cat "$EDGE_LOG_DIR/rag.pid")" "$EDGE_LOG_DIR/rag.log"
}

stop_rag_local() { stop_one "RAG" "$RAG_PORT" "$EDGE_LOG_DIR/rag.pid" 15; }

# ---- backend ----
start_backend() {
    if is_ready "$BACKEND_READY_URL"; then
        managed_or_report "backend" "$BACKEND_PORT" "$EDGE_LOG_DIR/backend.pid" backend || return 1
        info "backend 已在运行，跳过"; return 0
    fi
    guard_port_before_spawn "backend" "$BACKEND_PORT" "$EDGE_LOG_DIR/backend.pid" backend \
        "$BACKEND_READY_URL" "$BACKEND_TIMEOUT" "$EDGE_LOG_DIR/backend.log"
    case $? in 1) return 1 ;; 2) return 0 ;; esac
    [ -x "$EDGE_BACKEND_PY" ] || { fail "缺少 backend 解释器（${EDGE_BACKEND_PY}）${EDGE_SETUP_HINT:+，$EDGE_SETUP_HINT}"; return 1; }
    # 密钥盘点（只记有无+长度，不记内容）：401 类故障的第一现场证据。必须在
    # 一切门（可达性/鉴权）**之前**记——门一拦，事后就没有「注入了什么」的证据
    #（实机查验第三轮实证：鉴权门拦停后 backend.log 里空空如也）。
    key_stat() { if [ -n "$1" ]; then echo "有(${#1}字)"; else echo "无"; fi; }
    echo "[edge] $(date '+%H:%M:%S') 密钥盘点: AGENT=$(key_stat "${AGENT_API_KEY:-}") INFER=$(key_stat "${INFER_API_KEY:-}") RAG=$(key_stat "${RAG_API_KEY:-}")" \
        >>"$EDGE_LOG_DIR/backend.log"
    # 硬性依赖顺序：RAG 完全就绪后 backend 才能启动（工具探活只发生在 backend 启动时）。
    # edge profile 下 RAG_READY_URL 已指向设备 A（rag.host），这一条同样是跨机可达性校验。
    if ! is_ready "$RAG_READY_URL"; then
        fail "RAG 未就绪（探活 ${RAG_READY_URL}），拒绝启动 backend（否则 RAG 工具集不会注册）"
        [ "$(probe_host "$RAG_HOST")" = "127.0.0.1" ] || \
            warn "  RAG 配置在远端——请确认设备 A 上 './devctl.sh start --profile gpu' 已跑起来、Tailscale 可达。"
        return 1
    fi
    probe_auth "RAG" "$RAG_AUTH_URL" "${RAG_API_KEY:-}" || return 1
    # 模型预取（前台）：初次部署要下数 GB，HF 进度条直接显示在终端，杜绝「启动中假死」观感；
    # 已就绪时命中标记瞬时通过。backend 内部的下载路径保留为绕过启动器直启时的自愈兜底。
    # mode=remote：推理在设备 A，本机只需分词器（§S.3 B1）——按 config 的 mode 判，而非 profile 旗标，
    # 因为"要不要权重"取决于承载模式本身（同机 remote 双进程也一样不必在 backend 侧再拉一份）。
    if [ "$INFER_MODE" = "remote" ]; then
        info "检查分词器（inference.mode=remote：推理在远端，本机不拉权重）…"
        ( cd "$EDGE_BACKEND_DIR" && "$EDGE_BACKEND_PY" scripts/fetch_model.py --tokenizer-only ) \
            || { fail "分词器未就绪，中止 backend 启动"; return 1; }
        if ! is_ready "$INFER_READY_URL"; then
            fail "inference-server 未就绪（探活 ${INFER_READY_URL}），拒绝启动 backend"
            warn "  mode=remote 下没有推理承载，任何生成都会整轮失败。"
            warn "  请确认设备 A 上 './devctl.sh start --profile gpu' 已跑起来、Tailscale 可达。"
            return 1
        fi
        probe_auth "inference-server" "$INFER_AUTH_URL" "${INFER_API_KEY:-}" || return 1
    else
        info "检查模型（未下载时将前台下载并显示进度）…"
        ( cd "$EDGE_BACKEND_DIR" && "$EDGE_BACKEND_PY" scripts/fetch_model.py ) \
            || { fail "模型未就绪，中止 backend 启动"; return 1; }
    fi
    info "启动 backend (port $BACKEND_PORT) → $EDGE_LOG_DIR/backend.log"
    spawn_daemon "$EDGE_BACKEND_DIR" "$EDGE_LOG_DIR/backend.log" "$EDGE_LOG_DIR/backend.pid" \
        "$EDGE_BACKEND_PY" app.py || return 1
    wait_ready "backend" "$BACKEND_READY_URL" "$BACKEND_TIMEOUT" "$(cat "$EDGE_LOG_DIR/backend.pid")" "$EDGE_LOG_DIR/backend.log"
}

stop_backend() { stop_one "backend" "$BACKEND_PORT" "$EDGE_LOG_DIR/backend.pid" 20; }

# ---- edge 编队（app 与 devctl edge profile 共用的顺序与容错）----
# 🔴 SearXNG 必须先于 backend：web_research 的注册是 backend 启动期一次性探活，
# searx 晚到 = 整轮不注册（§S.3 已入账的事故）。searx 本身可选：起不来只告警不阻断。
edge_up() {
    edge_require_env || return 1
    mkdir -p "$EDGE_LOG_DIR"
    # RAG 数据面最先（B2c）：backend 启动门硬依赖 RAG 就绪（工具探活一次性）。
    # rag.host 指远端（拆分切换前的过渡拓扑）时不归本编队，交由 backend 门探活跨机校验。
    if rag_is_local; then
        start_rag_local || return 1
    fi
    { start_searx || warn "SearXNG 未就绪，web_research 将不注册，继续"; true; } && start_backend
}

edge_down() {
    edge_require_env || return 1
    stop_backend
    stop_searx
    if rag_is_local && [ -n "${EDGE_RAG_PY:-}" ]; then
        stop_rag_local
    fi
}

# ---- 孤儿回收（D 阶段工程票）----
# 场景：app 被 SIGKILL（不走 willTerminate）后，本脚本拉起的守护进程带着 pid 文件
# 活了下来；下一代 app 探活见「健康」便当 external 只用不管——孤儿从此永远跑着
# 已删除的旧 bundle 代码。判据是 pid 文件本身：**谁的数据根谁的兵**——bundle 布局的
# EDGE_LOG_DIR 在 app 专属数据根下，里面的 pid 记录必然是上代 app 经本脚本所拉。
# ⚠️ 仅 bundle 布局可用（SidecarManager 侧把关）：repo 布局与 devctl 共用 logs/，
# pid 文件无法区分归属，reap 会误杀用户的开发后端。
edge_reap() {
    edge_require_env || return 1
    local reaped=0
    local svc
    for svc in backend searx rag; do
        local pidfile="$EDGE_LOG_DIR/$svc.pid"
        [ -f "$pidfile" ] || continue
        local pid; pid="$(cat "$pidfile" 2>/dev/null)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            # ${} 必须显式界定：紧跟全角括号（多字节）会被 bash 吞首字节当变量名
            info "发现上代残留 ${svc}（pid ${pid}，pid 记录在本数据根），回收后重新拉起"
            case "$svc" in
                backend) stop_backend ;;
                searx)   stop_searx ;;
                rag)     stop_rag_local ;;
            esac
            reaped=$((reaped + 1))
        else
            rm -f "$pidfile"   # 记录已死：清掉陈尸的 pid 文件
        fi
    done
    # 第二判据：无档孤儿。pid 档案可能丢失（实机 2026-07-17 事故：孤儿 backend 无
    # pid 文件，探活「健康」被误判 external，从此永远只连不管、跑着旧代码）——
    # 端口占用者的解释器路径属于本布局（bundle 的 Resources/python）即是自家兵：
    # devctl/repo 后端绝不会用 bundle 解释器，按路径认领不存在误杀面。
    local port py p cmd
    for svc in backend searx rag; do
        case "$svc" in
            backend) port="$BACKEND_PORT" py="$EDGE_BACKEND_PY" ;;
            searx)   port="$SEARX_PORT"   py="$EDGE_SEARX_PY" ;;
            rag)     port="$RAG_PORT"     py="${EDGE_RAG_PY:-}"
                     [ -n "$py" ] || continue ;;   # 本布局无 rag 承载：无从按解释器认领
        esac
        for p in $(listen_pids "$port"); do
            cmd="$(ps -o command= -p "$p" 2>/dev/null)"
            case "$cmd" in
                "$py"*)
                    info "发现无档孤儿 ${svc}（pid ${p}，解释器属本布局），回收后重新拉起"
                    kill "$p" 2>/dev/null
                    local waited=0
                    while [ "$waited" -lt 20 ] && kill -0 "$p" 2>/dev/null; do
                        sleep 1; waited=$((waited + 1))
                    done
                    kill -0 "$p" 2>/dev/null && { warn "${svc} pid ${p} 未响应 TERM，强制结束"; kill -9 "$p" 2>/dev/null; }
                    reaped=$((reaped + 1))
                    ;;
            esac
        done
    done
    [ "$reaped" -eq 0 ] && info "无上代残留进程"
    return 0
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
        *)
            echo "用法: EDGE_*=... $0 up|down|reap|status（布局参数见文件头）" >&2
            exit 1
            ;;
    esac
fi
