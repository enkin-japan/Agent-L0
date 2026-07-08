#!/bin/bash
# =======================================================
# devctl.sh — MacAgent 单仓标准启动器（唯一入口）
#
# 目录约定（monorepo）:
#   backend/   Agent 主体（FastAPI + MLX, :14159）
#   rag/       RAG 知识库（FastAPI + PyTorch, :14160）
#   frontend/  Web 前端（Vite, :5173）
#   searxng/   SearXNG 配置挂载目录（docker 容器, :8080, 可选）
#   config.yaml     全项目唯一配置（backend 与 rag 共读）
#   .env(.local)    密钥/环境变量（backend 与前端代理共读, 本脚本负责 source）
#
# 用法:
#   ./devctl.sh setup                                # 首次部署: 建 venv + 装依赖 + npm install
#   ./devctl.sh start|stop|restart [rag|searx|backend|front|all]   # 缺省 all
#   ./devctl.sh status
#   ./devctl.sh logs [rag|searx|backend|front]       # tail 对应日志
#
# 启动顺序: RAG → SearXNG(docker) → backend → (等待就绪) → 前端
# 停止顺序: 前端 → backend → SearXNG → RAG
#
# SearXNG 为可选联网搜索层（web_research 工具依赖）：缺 docker 时自动跳过、
# 不阻断其余服务（此时 backend 的 web_research 探活失败、优雅不注册）。
#
# 典型远程用法（单条指令，进程以 nohup 常驻，ssh 退出不受影响）:
#   ssh <host> '/Volumes/enkin/project/MacAgent/devctl.sh restart'
# =======================================================
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

# ssh 非交互 shell 的 PATH 往往不含 homebrew（node/npm）；lsof 在 /usr/sbin
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/sbin:/sbin:${PATH:-/usr/bin:/bin}"

# nvm 只在交互 shell（.zshrc）里初始化，非交互 ssh 下手动把最新版 node 加入 PATH
if ! command -v npm >/dev/null 2>&1; then
    NVM_NODE_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1)"
    [ -n "$NVM_NODE_BIN" ] && export PATH="$NVM_NODE_BIN:$PATH"
fi

# 非交互 ssh 常常没有 locale（C locale），bash 会按字节误解析中文字符，
# 甚至把「$VAR全角字符」吞成不存在的变量名，这里统一兜底为 UTF-8
export LANG="${LANG:-en_US.UTF-8}"
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

# ---- 环境变量（密钥统一在仓库根，backend 与前端代理共用）----
# .env 先加载、.env.local 后加载覆盖；set -a 使其中的赋值自动 export 给子进程
# （backend 读 AGENT_API_KEY 启用鉴权；vite.config.ts 优先取进程环境变量）。
for envfile in "$ROOT/.env" "$ROOT/.env.local"; do
    if [ -f "$envfile" ]; then
        set -a; . "$envfile"; set +a
    fi
done

# ---- 服务定义（端口与 config.yaml / vite.config.ts 保持一致）----
RAG_DIR="$ROOT/rag"
RAG_PORT=14160
RAG_READY_URL="http://127.0.0.1:${RAG_PORT}/health"
RAG_TIMEOUT="${RAG_TIMEOUT:-600}"        # 嵌入/重排模型加载可能较慢

# SearXNG：本机联网搜索层（docker 容器）。端口须与 config.yaml 的 web_search.searxng_url 一致。
SEARX_DIR="$ROOT/searxng"                # settings.yml 挂载目录（首次由脚本生成）
SEARX_PORT=8080
SEARX_NAME="searxng"                     # docker 容器名
SEARX_IMAGE="searxng/searxng:latest"
SEARX_READY_URL="http://127.0.0.1:${SEARX_PORT}/"
SEARX_TIMEOUT="${SEARX_TIMEOUT:-60}"     # 仅等容器启动就绪；首次镜像拉取在 docker run 内同步完成

BACKEND_DIR="$ROOT/backend"
BACKEND_PORT=14159
BACKEND_READY_URL="http://127.0.0.1:${BACKEND_PORT}/health"   # 免鉴权健康探活（启用 API key 后 /api/* 会 401）
BACKEND_TIMEOUT="${BACKEND_TIMEOUT:-600}"    # 首次可能触发模型下载

FRONT_DIR="$ROOT/frontend"
FRONT_PORT=5173
FRONT_READY_URL="http://127.0.0.1:${FRONT_PORT}/"
FRONT_TIMEOUT="${FRONT_TIMEOUT:-60}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[devctl]${NC} $*"; }
warn()  { echo -e "${YELLOW}[devctl]${NC} $*"; }
fail()  { echo -e "${RED}[devctl]${NC} $*" >&2; }

# ---- 通用工具 ----
listen_pids() {  # $1: port
    lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null
}

is_ready() {  # $1: url
    curl -sf -m 3 -o /dev/null "$1"
}

# 后台拉起常驻进程。
# 注意：不能用 ( ... & ) 子壳包裹 —— macOS 自带 bash 3.2 的子壳会 wait4 等待
# 后台子进程，而子壳又继承 ssh 会话的 stdout 管道，导致远程单条指令
# `ssh <host> 'devctl.sh start'` 在服务存活期间永不返回。
# 在主 shell 直接 nohup + & 则脚本退出时不等待后台任务，进程被 launchd 收养。
spawn_daemon() {  # $1: 工作目录  $2: 日志  $3: pid 文件  $4...: 命令
    local dir="$1" log="$2" pidfile="$3"
    shift 3
    cd "$dir" || return 1
    nohup "$@" >>"$log" 2>&1 </dev/null &
    echo $! >"$pidfile"
    disown 2>/dev/null || true
    cd "$ROOT" || return 1
}

# 轮询就绪探针；期间守护进程若已退出则立即失败并展示日志尾部
wait_ready() {  # $1: 名称  $2: url  $3: 超时秒  $4: pid  $5: 日志文件
    local name="$1" url="$2" timeout="$3" pid="$4" log="$5" waited=0
    printf "[devctl] 等待 %s 就绪 " "$name"
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

# ---- 首次部署（setup）：Python venv + 依赖 + 前端 node_modules ----
# 吸收自各服务原先的 start_server.sh（已裁撤）：部署与启动分离，部署只在 setup 做一次。
MIN_PY="3.11"; MAX_PY="3.13"

best_system_python() {
    local best_py="" best_ver="0.0" candidates=() p ver ok better
    for p in $(which -a python3 python 2>/dev/null); do candidates+=("$p"); done
    candidates+=("/opt/homebrew/bin/python3" "$HOME/miniforge3/bin/python3" "/usr/local/bin/python3")
    for p in "${candidates[@]}"; do
        [ -x "$p" ] || continue
        case "$p" in *venv*|*.virtualenvs*) continue ;; esac  # 排除别的项目的虚拟环境
        ver=$("$p" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null) || continue
        [ -n "$ver" ] || continue
        ok=$(awk -v v="$ver" -v min="$MIN_PY" -v max="$MAX_PY" 'BEGIN{print (v>=min && v<=max)}')
        [ "$ok" -eq 1 ] || continue
        better=$(awk -v v="$ver" -v b="$best_ver" 'BEGIN{print (v>b)}')
        [ "$better" -eq 1 ] && { best_ver="$ver"; best_py="$p"; }
    done
    echo "$best_py"
}

setup_venv() {  # $1: 服务目录（含 requirements.txt）  $2: 名称
    local dir="$1" name="$2"
    if [ -x "$dir/.venv/bin/python" ]; then
        info "$name 的 .venv 已存在，跳过（如需重装依赖: $dir/.venv/bin/pip install -r $dir/requirements.txt）"
        return 0
    fi
    local py; py="$(best_system_python)"
    [ -n "$py" ] || { fail "系统中未找到版本介于 $MIN_PY ~ $MAX_PY 的 Python"; return 1; }
    info "$name: 用 $py 创建 .venv 并安装依赖（可能需要一段时间）…"
    "$py" -m venv "$dir/.venv" || return 1
    "$dir/.venv/bin/pip" install --upgrade pip -q || return 1
    "$dir/.venv/bin/pip" install -r "$dir/requirements.txt" || return 1
    info "$name 环境部署完毕 ✅"
}

do_setup() {
    setup_venv "$RAG_DIR" "RAG" || return 1
    setup_venv "$BACKEND_DIR" "backend" || return 1
    if [ -d "$FRONT_DIR/node_modules" ]; then
        info "前端 node_modules 已存在，跳过（如需重装: cd frontend && npm install）"
    else
        command -v npm >/dev/null || { fail "找不到 npm（PATH=${PATH}）"; return 1; }
        info "前端: npm install …"
        (cd "$FRONT_DIR" && npm install) || return 1
        info "前端依赖部署完毕 ✅"
    fi
    [ -f "$ROOT/.env.local" ] || warn "未发现 .env.local——如需启用后端鉴权，请: cp .env.example .env.local 并填写 AGENT_API_KEY"
    info "setup 完成。启动: ./devctl.sh start"
}

# ---- 启动 ----
start_rag() {
    if is_ready "$RAG_READY_URL"; then info "RAG 已在运行，跳过"; return 0; fi
    [ -x "$RAG_DIR/.venv/bin/python" ] || { fail "缺少 rag/.venv，请先运行: ./devctl.sh setup"; return 1; }
    info "启动 RAG (port $RAG_PORT) → $LOG_DIR/rag.log"
    spawn_daemon "$RAG_DIR" "$LOG_DIR/rag.log" "$LOG_DIR/rag.pid" .venv/bin/python rag_server.py || return 1
    wait_ready "RAG" "$RAG_READY_URL" "$RAG_TIMEOUT" "$(cat "$LOG_DIR/rag.pid")" "$LOG_DIR/rag.log"
}

# 确保 SearXNG 配置存在且开启 json 输出（web_research 靠 json 拿结构化结果）
ensure_searx_settings() {
    mkdir -p "$SEARX_DIR"
    local sf="$SEARX_DIR/settings.yml"
    if [ -f "$sf" ]; then
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
# 由 devctl.sh 生成：仅覆写必要项，其余继承 SearXNG 默认设置
use_default_settings: true
server:
  secret_key: "$secret"
  limiter: false          # 本机自用可关；对外暴露务必开启
search:
  formats:
    - html
    - json                # web_research 靠 json 输出拿结构化结果，必须开启
EOF
    info "已生成 SearXNG 配置: ${sf}（已开启 html + json 输出）"
}

wait_ready_searx() {
    local waited=0
    printf "[devctl] 等待 SearXNG 就绪 "
    while ! is_ready "$SEARX_READY_URL"; do
        if [ "$(docker inspect -f '{{.State.Running}}' "$SEARX_NAME" 2>/dev/null)" != "true" ]; then
            echo ""; fail "SearXNG 容器已退出，最近日志："; docker logs --tail 30 "$SEARX_NAME" >&2 2>&1; return 1
        fi
        if [ "$waited" -ge "$SEARX_TIMEOUT" ]; then
            echo ""; fail "SearXNG 在 ${SEARX_TIMEOUT}s 内未就绪，最近日志："; docker logs --tail 30 "$SEARX_NAME" >&2 2>&1; return 1
        fi
        sleep 2; waited=$((waited + 2)); printf "."
    done
    echo ""
    info "SearXNG 就绪 ✅ ($SEARX_READY_URL)"
}

start_searx() {
    if is_ready "$SEARX_READY_URL"; then info "SearXNG 已在运行，跳过"; return 0; fi
    command -v docker >/dev/null 2>&1 || { warn "未找到 docker，跳过 SearXNG（web_research 将不注册）"; return 1; }
    docker info >/dev/null 2>&1 || { warn "docker 守护进程未运行（请先启动 Docker Desktop/colima），跳过 SearXNG"; return 1; }
    ensure_searx_settings
    docker rm -f "$SEARX_NAME" >/dev/null 2>&1 || true   # 清理同名残留容器
    info "启动 SearXNG (port $SEARX_PORT) → docker 容器 ${SEARX_NAME}（首次会拉取镜像，请稍候）"
    # 仅绑本机回环：SearXNG 只供同机的 web_research 沙箱调用（limiter 已关，
    # 绑 0.0.0.0 会让局域网任何设备白嫖这个搜索代理）
    if ! docker run -d --name "$SEARX_NAME" -p "127.0.0.1:${SEARX_PORT}:8080" \
            -v "$SEARX_DIR:/etc/searxng" --restart unless-stopped \
            "$SEARX_IMAGE" >>"$LOG_DIR/searx.log" 2>&1; then
        fail "docker run 失败，最近日志（$LOG_DIR/searx.log）："; tail -n 20 "$LOG_DIR/searx.log" >&2; return 1
    fi
    wait_ready_searx
}

start_backend() {
    if is_ready "$BACKEND_READY_URL"; then info "backend 已在运行，跳过"; return 0; fi
    [ -x "$BACKEND_DIR/.venv/bin/python" ] || { fail "缺少 backend/.venv，请先运行: ./devctl.sh setup"; return 1; }
    # 硬性依赖顺序：RAG 完全就绪后 backend 才能启动（工具探活只发生在 backend 启动时）
    if ! is_ready "$RAG_READY_URL"; then
        fail "RAG 未就绪，拒绝启动 backend（否则 RAG 工具集不会注册）"
        return 1
    fi
    info "启动 backend (port $BACKEND_PORT) → $LOG_DIR/backend.log"
    spawn_daemon "$BACKEND_DIR" "$LOG_DIR/backend.log" "$LOG_DIR/backend.pid" .venv/bin/python app.py || return 1
    wait_ready "backend" "$BACKEND_READY_URL" "$BACKEND_TIMEOUT" "$(cat "$LOG_DIR/backend.pid")" "$LOG_DIR/backend.log"
}

start_front() {
    if is_ready "$FRONT_READY_URL"; then info "前端已在运行，跳过"; return 0; fi
    command -v npm >/dev/null || { fail "找不到 npm（PATH=${PATH}）"; return 1; }
    [ -d "$FRONT_DIR/node_modules" ] || { fail "缺少 frontend/node_modules，请先运行: ./devctl.sh setup"; return 1; }
    info "启动前端 (port $FRONT_PORT) → $LOG_DIR/front.log"
    spawn_daemon "$FRONT_DIR" "$LOG_DIR/front.log" "$LOG_DIR/front.pid" npm run dev || return 1
    wait_ready "前端" "$FRONT_READY_URL" "$FRONT_TIMEOUT" "$(cat "$LOG_DIR/front.pid")" "$LOG_DIR/front.log"
}

# ---- 停止 ----
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

stop_front()   { stop_one "前端"    "$FRONT_PORT"   "$LOG_DIR/front.pid"   5;  }
stop_backend() { stop_one "backend" "$BACKEND_PORT" "$LOG_DIR/backend.pid" 20; }
stop_rag()     { stop_one "RAG"     "$RAG_PORT"     "$LOG_DIR/rag.pid"     15; }

stop_searx() {  # docker 容器，不走端口 pid 那套（-p 映射下 lsof 只见 docker-proxy）
    command -v docker >/dev/null 2>&1 || { info "SearXNG 未运行（无 docker）"; return 0; }
    if [ -n "$(docker ps -aq -f "name=^${SEARX_NAME}$" 2>/dev/null)" ]; then
        info "停止 SearXNG 容器 $SEARX_NAME"
        docker rm -f "$SEARX_NAME" >/dev/null 2>&1 || true
        info "SearXNG 已停止"
    else
        info "SearXNG 未在运行"
    fi
}

# ---- 状态 ----
status_one() {  # $1: 名称  $2: 端口  $3: 就绪 url
    local pids; pids="$(listen_pids "$2" | tr '\n' ' ')"
    if [ -n "$pids" ]; then
        if is_ready "$3"; then
            echo -e "  ${GREEN}●${NC} $1\tport $2\tpid $pids\t就绪"
        else
            echo -e "  ${YELLOW}●${NC} $1\tport $2\tpid $pids\t启动中/未就绪"
        fi
    else
        echo -e "  ${RED}●${NC} $1\tport $2\t未运行"
    fi
}

status_searx() {  # docker 容器：优先看就绪，其次看容器是否在跑
    if is_ready "$SEARX_READY_URL"; then
        echo -e "  ${GREEN}●${NC} SearXNG \tport $SEARX_PORT\t(docker $SEARX_NAME)\t就绪"
    elif command -v docker >/dev/null 2>&1 && [ -n "$(docker ps -q -f "name=^${SEARX_NAME}$" 2>/dev/null)" ]; then
        echo -e "  ${YELLOW}●${NC} SearXNG \tport $SEARX_PORT\t启动中/未就绪"
    else
        echo -e "  ${RED}●${NC} SearXNG \tport $SEARX_PORT\t未运行（可选）"
    fi
}

do_status() {
    echo "[devctl] 服务状态："
    status_one "RAG     " "$RAG_PORT" "$RAG_READY_URL"
    status_searx
    status_one "backend " "$BACKEND_PORT" "$BACKEND_READY_URL"
    status_one "前端    " "$FRONT_PORT" "$FRONT_READY_URL"
    echo "[devctl] 日志目录: $LOG_DIR"
}

do_logs() {
    case "${1:-all}" in
        rag)     tail -n 50 "$LOG_DIR/rag.log" ;;
        searx)   docker logs --tail 50 "$SEARX_NAME" 2>&1 || echo "(SearXNG 未运行或无 docker)" ;;
        backend) tail -n 50 "$LOG_DIR/backend.log" ;;
        front)   tail -n 50 "$LOG_DIR/front.log" ;;
        all)     echo "===== rag ====="; tail -n 20 "$LOG_DIR/rag.log" 2>/dev/null || echo "(无日志)"
                 echo "===== searx ====="; docker logs --tail 20 "$SEARX_NAME" 2>&1 || echo "(SearXNG 未运行或无 docker)"
                 for f in backend front; do
                     echo "===== $f ====="; tail -n 20 "$LOG_DIR/$f.log" 2>/dev/null || echo "(无日志)"
                 done ;;
        *)       fail "未知服务: ${1}（可选 rag|searx|backend|front）"; exit 1 ;;
    esac
}

# ---- 命令分发 ----
CMD="${1:-}"
TARGET="${2:-all}"
# 兼容旧服务名（拆分仓时代）：agent → backend
[ "$TARGET" = "agent" ] && TARGET="backend"

case "$CMD" in
    setup) do_setup ;;
    start)
        case "$TARGET" in
            # SearXNG 为可选层：起不来只告警不阻断（web_research 会优雅不注册），故不并入 && 链
            all)     start_rag && { start_searx || warn "SearXNG 未就绪，web_research 将不注册，继续启动其余服务"; true; } && start_backend && start_front ;;
            rag)     start_rag ;;
            searx)   start_searx ;;
            backend) start_backend ;;   # 内部会校验 RAG 已就绪
            front)   start_front ;;
            *) fail "未知服务: ${TARGET}（可选 rag|searx|backend|front|all）"; exit 1 ;;
        esac
        ;;
    stop)
        case "$TARGET" in
            all)     stop_front; stop_backend; stop_searx; stop_rag ;;
            rag)     stop_rag ;;
            searx)   stop_searx ;;
            backend) stop_backend ;;
            front)   stop_front ;;
            *) fail "未知服务: ${TARGET}（可选 rag|searx|backend|front|all）"; exit 1 ;;
        esac
        ;;
    restart)
        "$0" stop "$TARGET" && "$0" start "$TARGET"
        exit $?
        ;;
    status) do_status ;;
    logs)   do_logs "$TARGET" ;;
    *)
        echo "用法: $0 setup                                   # 首次部署（venv/依赖/node_modules）"
        echo "      $0 start|stop|restart [rag|searx|backend|front|all]"
        echo "      $0 status"
        echo "      $0 logs [rag|searx|backend|front]"
        echo "远程示例: ssh <host> '$ROOT/devctl.sh restart'"
        exit 1
        ;;
esac
