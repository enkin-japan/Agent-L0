#!/bin/bash
# =======================================================
# devctl.sh — Agent 单仓标准启动器（唯一入口）
#
# 目录约定（monorepo）:
#   backend/   Agent 主体（FastAPI, :14159）
#   inference/ 推理服务（FastAPI, :14161, 仅 inference.mode=remote / --profile gpu 时跑）
#   rag/       RAG 知识库（FastAPI, :14160 数据面；B2c 拆分时另有 :14162 计算面，
#              rag/compute_server.py，torch/transformers 归计算面——按 rag.compute.host 分派）
#   frontend/  Web 前端（Vite, :5173）
#   searxng/   SearXNG 原生部署目录（settings.yml + searxng-src 源码 + .venv, :8080, 可选）
#   config.yaml     全项目唯一配置（backend 与 rag 共读）
#   .env(.local)    密钥/环境变量（backend 与前端代理共读, 本脚本负责 source）
#
# 用法:
#   ./devctl.sh setup                                # 首次部署: 建 venv + 装依赖 + npm install
#   ./devctl.sh model                                # 从注册表选择启动模型（写 .model-selection）
#   ./devctl.sh rag-model                            # 从 rag.tiers 选择 RAG 档位（写 .rag-tier-selection）
#   ./devctl.sh start|stop|restart [rag|searx|backend|front|all]   # 缺省 all
#   ./devctl.sh status
#   ./devctl.sh logs [rag|searx|backend|front]       # tail 对应日志
#
# 启动顺序: RAG → SearXNG(原生 venv) → backend → (等待就绪) → 前端
# 停止顺序: 前端 → backend → SearXNG → RAG
#
# SearXNG 为可选联网搜索层（web_research 工具依赖）：原生 Python 部署、不依赖 Docker——
# setup 克隆源码到 searxng/searxng-src（pin 提交）并建 searxng/.venv；环境缺失时自动跳过、
# 不阻断其余服务（此时 backend 的 web_research 探活失败、优雅不注册）。
#
# 典型远程用法（单条指令，进程以 nohup 常驻，ssh 退出不受影响）:
#   ssh <host> '/Volumes/enkin/project/Agent/devctl.sh restart'
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

# 非交互 ssh 常常没有 locale（C locale），bash 会按字节误解析中文字符，这里兜底为 UTF-8。
# ⚠️ 但它挡不住「$VAR全角字符」：macOS 自带 bash 3.2 仍会把紧跟的多字节首字节吞进变量名
# （set -u 下报 unbound variable）。凡变量后紧跟中文，一律写 ${VAR} 显式界定——这是硬规矩，
# 不是 locale 能兜的（实证：LC_ALL 已置 UTF-8，"$LOG_DIR。" 照样炸）。
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

# ---- 服务定义（端口常量与就绪判据在 scripts/lib_edge.sh，单一真相）----
RAG_DIR="$ROOT/rag"

# SearXNG：本机联网搜索层（原生 Python，无 Docker）。运行期归 lib_edge.sh；
# 这里只留 setup（克隆/venv）所需的部署常量。
SEARX_DIR="$ROOT/searxng"                # settings.yml + 源码 + venv 所在目录
SEARX_SRC="$SEARX_DIR/searxng-src"       # 源码克隆（pip -e 的安装源，setup 时拉取）
SEARX_VENV="$SEARX_DIR/.venv"
SEARX_REPO="https://github.com/searxng/searxng"
# 源码 pin（SEARX_COMMIT）收编在 lib_edge.sh：devctl setup 与 build-runtime.sh 共用一份

BACKEND_DIR="$ROOT/backend"

FRONT_DIR="$ROOT/frontend"
FRONT_PORT=5173
FRONT_READY_URL="http://127.0.0.1:${FRONT_PORT}/"
FRONT_TIMEOUT="${FRONT_TIMEOUT:-60}"

# inference-server（HETERO_COMPUTE B1/D7，:14161）：gpu profile 的推理承载。
# 无独立 venv——跑在 backend 的 venv 里（B1 实证形态：cd backend && .venv/bin/python ../inference/server.py）。
INFER_DIR="$ROOT/inference"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[devctl]${NC} $*"; }
warn()  { echo -e "${YELLOW}[devctl]${NC} $*"; }
fail()  { echo -e "${RED}[devctl]${NC} $*" >&2; }

# ---- edge 启动库（R5-4/D.13）：启什么/什么顺序/怎么算就绪的单一真相 ----
# 仓库布局参数在此注入；macOS app 以 bundle 布局参数直接执行同一文件（up|down|status）。
# 端口/超时常量、探活机制、SearXNG 配置生成、start/stop backend+searx 全在库里。
EDGE_BACKEND_DIR="$BACKEND_DIR"
EDGE_BACKEND_PY="$BACKEND_DIR/.venv/bin/python"
EDGE_SEARX_DIR="$SEARX_DIR"
EDGE_SEARX_PY="$SEARX_VENV/bin/python"
EDGE_SEARX_SETTINGS="$SEARX_DIR/settings.yml"
EDGE_LOG_DIR="$LOG_DIR"
EDGE_CONFIG="$ROOT/config.yaml"
EDGE_SETUP_HINT="请先运行: ./devctl.sh setup"
EDGE_STOP_CMD="$0 stop"
# RAG 数据面承载（B2c）：仓库布局用 rag/.venv（融合模式的模型也在这套 venv 里；
# 拆分模式的数据面不 import torch，同一解释器两模式通用）。数据根缺省 rag/ 本目录。
EDGE_RAG_DIR="$RAG_DIR"
EDGE_RAG_PY="$RAG_DIR/.venv/bin/python"
. "$ROOT/scripts/lib_edge.sh" || { fail "无法加载 scripts/lib_edge.sh"; exit 1; }



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

# SearXNG 原生部署（可选层）：克隆源码 → pin 提交 → venv → requirements + editable 安装。
# 失败只告警不阻断——SearXNG 缺席时 backend 的 web_research 探活失败、优雅不注册。
setup_searx() {
    local head=""
    [ -d "$SEARX_SRC/.git" ] && head="$(git -C "$SEARX_SRC" rev-parse HEAD 2>/dev/null)"
    if [ -x "$SEARX_VENV/bin/python" ] && [ "$head" = "$SEARX_COMMIT" ] \
            && "$SEARX_VENV/bin/python" -c "import searx" >/dev/null 2>&1; then
        info "SearXNG 环境已就绪（pin: ${SEARX_COMMIT:0:12}），跳过"
        return 0
    fi
    command -v git >/dev/null 2>&1 || { warn "未找到 git，跳过 SearXNG 部署"; return 1; }
    if [ ! -d "$SEARX_SRC/.git" ]; then
        info "SearXNG: 克隆源码 → $SEARX_SRC"
        git clone --depth 1 "$SEARX_REPO" "$SEARX_SRC" || { warn "SearXNG 源码克隆失败，跳过"; return 1; }
        head="$(git -C "$SEARX_SRC" rev-parse HEAD 2>/dev/null)"
    fi
    if [ "$head" != "$SEARX_COMMIT" ]; then
        # GitHub 支持按 SHA 浅取；失败（如网络受限）就用现有源码继续，只是不保证与实测版本一致
        (git -C "$SEARX_SRC" fetch --depth 1 origin "$SEARX_COMMIT" \
            && git -C "$SEARX_SRC" checkout -q "$SEARX_COMMIT") \
            || warn "SearXNG 无法切到 pin 提交 ${SEARX_COMMIT:0:12}，使用当前源码（${head:0:12}）"
    fi
    if [ ! -x "$SEARX_VENV/bin/python" ]; then
        local py; py="$(best_system_python)"
        [ -n "$py" ] || { warn "系统中未找到版本介于 $MIN_PY ~ $MAX_PY 的 Python，跳过 SearXNG"; return 1; }
        info "SearXNG: 用 $py 创建 .venv 并安装依赖（可能需要几分钟）…"
        "$py" -m venv "$SEARX_VENV" || { warn "SearXNG venv 创建失败"; return 1; }
        "$SEARX_VENV/bin/pip" install -q -U pip setuptools wheel pyyaml || { warn "SearXNG 基础构建工具安装失败"; return 1; }
    fi
    # setup.py 构建期会 import searx：必须先装齐 requirements，editable 安装关掉构建隔离
    "$SEARX_VENV/bin/pip" install -q -r "$SEARX_SRC/requirements.txt" || { warn "SearXNG 依赖安装失败"; return 1; }
    "$SEARX_VENV/bin/pip" install -q --no-build-isolation -e "$SEARX_SRC" || { warn "SearXNG editable 安装失败"; return 1; }
    info "SearXNG 环境部署完毕 ✅（源码 pin: ${SEARX_COMMIT:0:12}）"
}

do_setup() {
    setup_venv "$RAG_DIR" "RAG" || return 1
    setup_venv "$BACKEND_DIR" "backend" || return 1
    setup_searx || warn "SearXNG 部署未完成（可选层，缺席时 web_research 不注册；可稍后重跑 ./devctl.sh setup）"
    if [ -d "$FRONT_DIR/node_modules" ]; then
        info "前端 node_modules 已存在，跳过（如需重装: cd frontend && npm install）"
    else
        command -v npm >/dev/null || { fail "找不到 npm（PATH=${PATH}）"; return 1; }
        info "前端: npm install …"
        (cd "$FRONT_DIR" && npm install) || return 1
        info "前端依赖部署完毕 ✅"
    fi
    [ -f "$ROOT/.env.local" ] || warn "未发现 .env.local——如需启用后端鉴权，请: cp .env.example .env.local 并填写 AGENT_API_KEY"
    do_keygen
    info "setup 完成。启动: ./devctl.sh start"
}

# ---- 密钥生成（keygen）----
# 四把 Bearer 钥匙（AGENT/INFER/RAG/RAG_COMPUTE）的空值/弱值（长度<16，含占位 "test"）
# 一键换成强随机串——护栏规则「非回环绑定 + 密钥缺失或过弱 = 拒启」的配套逃生口。
# 目标文件默认仓库根 .env.local，可用 DEVCTL_ENV_FILE 覆盖（测试指临时文件，绝不碰真身）。
# upsert：只改这四个键、保留文件其余行原样；已是强值则原样保留（幂等）。chmod 600。
do_keygen() {
    local target="${DEVCTL_ENV_FILE:-$ROOT/.env.local}"
    if [ ! -f "$target" ]; then
        if [ -f "$ROOT/.env.example" ]; then
            cp "$ROOT/.env.example" "$target"
            info "已从 .env.example 创建 ${target}"
        else
            : > "$target"
            info "已创建空 ${target}"
        fi
    fi
    chmod 600 "$target" 2>/dev/null || true

    local key
    for key in AGENT_API_KEY INFER_API_KEY RAG_API_KEY RAG_COMPUTE_API_KEY; do
        _keygen_upsert "$target" "$key"
    done
    chmod 600 "$target" 2>/dev/null || true

    # 指引块：四键当前生效值 + 两条部署提示。
    echo ""
    info "密钥就绪（${target}，权限 600）。当前生效值："
    for key in AGENT_API_KEY INFER_API_KEY RAG_API_KEY RAG_COMPUTE_API_KEY; do
        printf '    %-20s = %s\n' "$key" "$(_keygen_read "$target" "$key")"
    done
    echo "  部署提示："
    echo "    • 算力端与工作端两侧 .env.local 四键必须同值（Bearer 两端对齐才能互认）。"
    echo "    • macOS/iOS app 在设置页填 INFER / RAG 两把密钥；后端地址始终是工作端自己的"
    echo "      http://127.0.0.1:14159（不是算力端端口）。"
    echo "    • 算力端 .env.local 还需填 INFER_REMOTE_HOST / RAG_COMPUTE_HOST=本机 tailnet IP"
    echo "      （config 两个 host 缺省为空，通用模板不钉机器；分离部署必填）。"
}

# 读单键当前值（去掉两侧引号；无则空）。仅取该键最后一次赋值。
_keygen_read() {  # $1: 文件  $2: 键名
    local v
    v="$(grep -E "^${2}=" "$1" 2>/dev/null | tail -n1)"
    v="${v#*=}"
    v="${v%\"}"; v="${v#\"}"
    v="${v%\'}"; v="${v#\'}"
    printf '%s' "$v"
}

# 空值/长度<16 → 生成强随机替换（upsert，保留其余行）；已达强度则原样保留。
_keygen_upsert() {  # $1: 文件  $2: 键名
    local file="$1" key="$2" cur
    cur="$(_keygen_read "$file" "$key")"
    if [ "${#cur}" -ge 16 ]; then
        return 0   # 已是强值，原样保留（幂等）
    fi
    local val
    val="$(python3 -c 'import secrets;print(secrets.token_hex(24))')" || { fail "生成随机密钥失败（缺 python3？）"; return 1; }
    if grep -qE "^${key}=" "$file" 2>/dev/null; then
        # 就地替换该键行（值含 / 也安全：用 | 作分隔且值是 hex，无特殊字符）
        /usr/bin/perl -i -pe "s|^${key}=.*|${key}=\"${val}\"|" "$file"
    else
        printf '%s="%s"\n' "$key" "$val" >> "$file"
    fi
    info "已为 ${key} 生成强随机值（原值空或长度<16）"
}

# ---- 启动 ----
# RAG（数据面或融合服务，按 config rag.compute.host 分派）：机制收编在 lib_edge.sh
# 的 start_rag_local（B2c——bundle 布局的 sidecar 走同一函数，编队不得两份）。
start_rag() { start_rag_local; }

# RAG 计算面（B2c 拆分，:14162，算力端）：无状态嵌入/重排/分词服务，跑在 rag/.venv
#（torch/transformers 在此）。仅 rag.compute.host 非空（拆分模式）时有它。
start_rag_compute() {
    if is_ready "$RAGC_READY_URL"; then
        managed_or_report "RAG计算面" "$RAGC_PORT" "$LOG_DIR/rag-compute.pid" rag-compute || return 1
        info "RAG 计算面已在运行，跳过"; return 0
    fi
    guard_port_before_spawn "RAG计算面" "$RAGC_PORT" "$LOG_DIR/rag-compute.pid" rag-compute \
        "$RAGC_READY_URL" "$RAG_TIMEOUT" "$LOG_DIR/rag-compute.log"
    case $? in 1) return 1 ;; 2) return 0 ;; esac
    [ -x "$RAG_DIR/.venv/bin/python" ] || { fail "缺少 rag/.venv，请先运行: ./devctl.sh setup"; return 1; }
    info "启动 RAG 计算面 (port $RAGC_PORT) → $LOG_DIR/rag-compute.log"
    rotate_log "$LOG_DIR/rag-compute.log"   # 本代日志起点：隔绝上一代进程日志混入诊断
    spawn_daemon "$RAG_DIR" "$LOG_DIR/rag-compute.log" "$LOG_DIR/rag-compute.pid" \
        .venv/bin/python compute_server.py || return 1
    wait_ready "RAG计算面" "$RAGC_READY_URL" "$RAG_TIMEOUT" \
        "$(cat "$LOG_DIR/rag-compute.pid")" "$LOG_DIR/rag-compute.log"
}


start_front() {
    if is_ready "$FRONT_READY_URL"; then
        managed_or_report "前端" "$FRONT_PORT" "$LOG_DIR/front.pid" front || return 1
        info "前端已在运行，跳过"; return 0
    fi
    command -v npm >/dev/null || { fail "找不到 npm（PATH=${PATH}）"; return 1; }
    [ -d "$FRONT_DIR/node_modules" ] || { fail "缺少 frontend/node_modules，请先运行: ./devctl.sh setup"; return 1; }
    info "启动前端 (port $FRONT_PORT) → $LOG_DIR/front.log"
    rotate_log "$LOG_DIR/front.log"   # 本代日志起点：隔绝上一代进程日志混入诊断
    spawn_daemon "$FRONT_DIR" "$LOG_DIR/front.log" "$LOG_DIR/front.pid" npm run dev || return 1
    wait_ready "前端" "$FRONT_READY_URL" "$FRONT_TIMEOUT" "$(cat "$LOG_DIR/front.pid")" "$LOG_DIR/front.log"
}

# ---- 停止（机制 stop_one 在 lib_edge.sh）----

stop_front()   { stop_one "前端"    "$FRONT_PORT"   "$LOG_DIR/front.pid"   5;  }
stop_rag()     { stop_rag_local; }
stop_rag_compute() { stop_one "RAG计算面" "$RAGC_PORT" "$LOG_DIR/rag-compute.pid" 15; }

# inference-server（gpu profile 的推理承载）。跑在 backend 的 venv 里（无独立 venv，B1 实证形态）；
# 工作目录取 BACKEND_DIR，故 framework.* 可直接 import。SIGTERM = uvicorn 优雅排水
# （在飞生成做完才退，见 §S.3 B1），故停机宽限给足。
start_infer() {
    if is_ready "$INFER_READY_URL"; then
        managed_or_report "inference-server" "$INFER_PORT" "$LOG_DIR/infer.pid" infer || return 1
        info "inference-server 已在运行，跳过"; return 0
    fi
    [ -x "$BACKEND_DIR/.venv/bin/python" ] || { fail "缺少 backend/.venv，请先运行: ./devctl.sh setup"; return 1; }
    # 空 INFER_HOST → 拒启（早于模型预取，fetch_model 很贵）：config 缺省为空，分离部署必填
    require_host "$INFER_HOST" "算力端（inference-server）" \
        ".env.local 写 INFER_REMOTE_HOST=<本机 tailnet IP>，或单机部署改 inference.mode: inprocess 并去掉 --profile gpu" \
        || return 1
    # 权重预取走与 backend 相同的前台下载器（进度条直显），gpu 侧要的是全量权重
    info "检查模型（未下载时将前台下载并显示进度）…"
    ( cd "$BACKEND_DIR" && .venv/bin/python scripts/fetch_model.py ) \
        || { fail "模型未就绪，中止 inference-server 启动"; return 1; }
    info "启动 inference-server (port $INFER_PORT) → $LOG_DIR/infer.log"
    rotate_log "$LOG_DIR/infer.log"   # 本代日志起点：隔绝上一代进程日志混入诊断
    spawn_daemon "$BACKEND_DIR" "$LOG_DIR/infer.log" "$LOG_DIR/infer.pid" \
        .venv/bin/python ../inference/server.py || return 1
    wait_ready "inference-server" "$INFER_READY_URL" "$INFER_TIMEOUT" \
        "$(cat "$LOG_DIR/infer.pid")" "$LOG_DIR/infer.log"
}
stop_infer()   { stop_one "inference-server" "$INFER_PORT" "$LOG_DIR/infer.pid" 30; }


# ---- 状态（status_one/status_searx 在 lib_edge.sh）----

do_status() {
    echo "[devctl] 服务状态（inference.mode=${INFER_MODE}）："
    status_one "RAG     " "$RAG_PORT" "$RAG_READY_URL"
    # B2c 拆分模式（rag.compute.host 非空）才有独立计算面；融合模式无此服务
    if [ -n "${RAGC_HOST:-}" ]; then
        status_one "RAG计算 " "$RAGC_PORT" "$RAGC_READY_URL"
    fi
    status_searx
    # 仅 remote 模式才该有独立推理服务：inprocess 下权重在 backend 进程内，
    # 这一行标红只会让人去查一个按设计就不存在的东西。
    if [ "$INFER_MODE" = "remote" ]; then
        status_one "推理服务" "$INFER_PORT" "$INFER_READY_URL"
    fi
    status_one "backend " "$BACKEND_PORT" "$BACKEND_READY_URL"
    status_one "前端    " "$FRONT_PORT" "$FRONT_READY_URL"
    echo "[devctl] 日志目录: $LOG_DIR"
}

do_logs() {
    case "${1:-all}" in
        rag)     tail -n 50 "$LOG_DIR/rag.log" ;;
        rag-compute) tail -n 50 "$LOG_DIR/rag-compute.log" 2>/dev/null || echo "(无日志)" ;;
        searx)   tail -n 50 "$LOG_DIR/searx.log" 2>/dev/null || echo "(无日志)" ;;
        infer)   tail -n 50 "$LOG_DIR/infer.log" 2>/dev/null || echo "(无日志)" ;;
        backend) tail -n 50 "$LOG_DIR/backend.log" ;;
        front)   tail -n 50 "$LOG_DIR/front.log" ;;
        all)     echo "===== rag ====="; tail -n 20 "$LOG_DIR/rag.log" 2>/dev/null || echo "(无日志)"
                 echo "===== searx ====="; tail -n 20 "$LOG_DIR/searx.log" 2>/dev/null || echo "(无日志)"
                 for f in rag-compute infer backend front; do
                     echo "===== $f ====="; tail -n 20 "$LOG_DIR/$f.log" 2>/dev/null || echo "(无日志)"
                 done ;;
        *)       fail "未知服务: ${1}（可选 rag|rag-compute|searx|infer|backend|front）"; exit 1 ;;
    esac
}

# ---- 模型选择：从 config.yaml 注册表交互选择，写仓库根 .model-selection 指针 ----
# 注册表条目全部钉死 revision（与 mlx-vlm 同代，见 config.yaml 注释），选择后重启 backend 生效。
# 非 TTY（ssh 脚本化）只打印当前状态不阻塞——自动化路径永远沿用上次选择。
do_model() {
    [ -x "$BACKEND_DIR/.venv/bin/python" ] || { fail "缺少 backend/.venv，请先运行: ./devctl.sh setup"; return 1; }
    MACAGENT_ROOT="$ROOT" "$BACKEND_DIR/.venv/bin/python" - <<'PY'
import json, os, sys
from pathlib import Path
import yaml

root = Path(os.environ["MACAGENT_ROOT"])
cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
inference = cfg.get("inference") or {}
registry = inference.get("model_registry") or []
default_repo = inference.get("model")
sel_file = root / ".model-selection"
try:
    current = json.loads(sel_file.read_text(encoding="utf-8")).get("repo")
except Exception:
    current = None

print("可用模型（config.yaml inference.model_registry，revision 已钉死）：")
for i, e in enumerate(registry, 1):
    marks = []
    if e["repo"] == (current or default_repo):
        marks.append("当前")
    if e["repo"] == default_repo:
        marks.append("默认")
    tag = f"  ← {'/'.join(marks)}" if marks else ""
    print(f"  {i:2d}. {e['repo'].split('/')[-1]:28s} {e.get('note','')}{tag}")

try:
    tty = open("/dev/tty", "r")
except OSError:
    print("（非交互环境：仅显示状态。交互选择请在终端运行 ./devctl.sh model）")
    sys.exit(0)

print("输入编号选择（回车=不变，0=清除选择恢复默认）: ", end="", flush=True)
line = tty.readline().strip()
if not line:
    print("保持不变。")
    sys.exit(0)
if line == "0":
    sel_file.unlink(missing_ok=True)
    print(f"已清除选择，回落默认: {default_repo}")
else:
    try:
        entry = registry[int(line) - 1]
    except (ValueError, IndexError):
        print(f"无效编号: {line!r}")
        sys.exit(1)
    sel_file.write_text(json.dumps({"repo": entry["repo"]}, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(f"已选择: {entry['repo']} @ {entry['revision'][:8]}")
print("重启 backend 生效: ./devctl.sh restart backend（新模型将在启动时前台下载并显示进度）")
PY
}

# ---- RAG 档位选择：从 config.yaml rag.tiers 交互选择，写仓库根 .rag-tier-selection 指针 ----
# 换档 = 选择，非手填（HETERO_COMPUTE B2a）。换档纪律：库一旦入库档位即固定，换到不同维度的
# 新档须重建向量库（rag/scripts/reembed.py）。非 TTY（脚本化）只打印状态不阻塞。
do_rag_model() {
    [ -x "$RAG_DIR/.venv/bin/python" ] || { fail "缺少 rag/.venv，请先运行: ./devctl.sh setup"; return 1; }
    MACAGENT_ROOT="$ROOT" "$RAG_DIR/.venv/bin/python" - <<'PY'
import json, os, sys
from pathlib import Path
import yaml

root = Path(os.environ["MACAGENT_ROOT"])
cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
rag = cfg.get("rag") or {}
tiers = rag.get("tiers") or []
default_id = rag.get("active_tier")
sel_file = root / ".rag-tier-selection"
try:
    current = json.loads(sel_file.read_text(encoding="utf-8")).get("tier")
except Exception:
    current = None

if not tiers:
    print("config.yaml rag.tiers 为空（走旧 embed_path/rerank_path 兜底）。")
    sys.exit(0)

print("可用 RAG 档位（config.yaml rag.tiers；档 = 一对 嵌入+重排 模型）：")
for i, t in enumerate(tiers, 1):
    marks = []
    if t["id"] == (current or default_id):
        marks.append("当前")
    if t["id"] == default_id:
        marks.append("默认")
    tag = f"  ← {'/'.join(marks)}" if marks else ""
    print(f"  {i:2d}. {t['id']:10s} [{t.get('modality','text'):4s}] {t.get('note','')}{tag}")

try:
    tty = open("/dev/tty", "r")
except OSError:
    print("（非交互环境：仅显示状态。交互选择请在终端运行 ./devctl.sh rag-model）")
    sys.exit(0)

print("输入编号选择（回车=不变，0=清除选择恢复默认）: ", end="", flush=True)
line = tty.readline().strip()
if not line:
    print("保持不变。")
    sys.exit(0)
if line == "0":
    sel_file.unlink(missing_ok=True)
    print(f"已清除选择，回落默认: {default_id}")
else:
    try:
        entry = tiers[int(line) - 1]
    except (ValueError, IndexError):
        print(f"无效编号: {line!r}")
        sys.exit(1)
    sel_file.write_text(json.dumps({"tier": entry["id"]}, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"已选择档位: {entry['id']}（{entry.get('embed_repo')} + {entry.get('rerank_repo')}）")
print("⚠️ 换档纪律：库一旦入库档位即固定。换到不同维度的新档需重建向量库——")
print("   见 rag/scripts/reembed.py（dump-categories → stop → wipe → start → rebuild）。")
print("重启 rag 生效: ./devctl.sh restart rag（新模型将在启动时下载并加载）")
PY
}

# ---- 命令分发 ----
CMD="${1:-}"
shift 2>/dev/null || true

# --profile edge|gpu（HETERO_COMPUTE B3）：把 all-in-one 拆成两台设备的角色。
#   （无 profile）= 现状 all-in-one，永不破坏——不传旗标时下面的分发与改动前逐字等价。
#   edge = 设备 B（用户 Mac）：backend + 前端；推理/RAG 在设备 A（config 的 host 指过去）。
#   gpu  = 设备 A（算力机）：inference-server + RAG (+ SearXNG)；不起前端、不起编排。
PROFILE=""
_args=""
while [ $# -gt 0 ]; do
    case "$1" in
        --profile) PROFILE="${2:-}"; shift 2 || true ;;
        --profile=*) PROFILE="${1#--profile=}"; shift ;;
        *) _args="$_args $1"; shift ;;
    esac
done
# shellcheck disable=SC2086
set -- $_args
TARGET="${1:-all}"
# 兼容旧服务名（拆分仓时代）：agent → backend
[ "$TARGET" = "agent" ] && TARGET="backend"

case "$PROFILE" in
    ""|edge|gpu) ;;
    *) fail "未知 profile: ${PROFILE}（可选 edge|gpu；不传 = all-in-one 现状）"; exit 1 ;;
esac

case "$CMD" in
    setup) do_setup ;;
    keygen) do_keygen ;;
    model) do_model ;;
    rag-model) do_rag_model ;;
    start)
        case "$PROFILE" in
            edge)
                # 设备 B：起编排 + 搜索层 + 前端。RAG/推理的可达性由 start_backend 内部校验
                # （RAG_READY_URL / INFER_READY_URL 已按 config 指向设备 A）。
                # SearXNG 归 edge 而非 gpu：它不是算力（无模型无 GPU），而是 web_research 的
                # 工具依赖，且抓正文的 T0/T1/T2 三档本就在 backend 里跑——出网口已经在 B，
                # 把「搜」放 A、「抓」放 B 是把一件事劈成两半。同机即回环，searxng_url 无需
                # 改动，也就不必给这个无鉴权、limiter 已关的服务开 tailnet 端口。
                # 顺序（searx 先于 backend）与容错收编在 lib_edge.sh 的 edge_up（D.13）——
                # macOS app 执行的是同一个函数，此处不得另写一份编队。
                info "profile=edge（设备 B：SearXNG + backend + 前端；推理/RAG 在远端）"
                [ "$INFER_MODE" = "remote" ] || warn "inference.mode=${INFER_MODE}——edge 通常配 remote，否则推理仍在本机跑。"
                edge_up && start_front
                exit $?
                ;;
            gpu)
                # 设备 A：只起重型计算。不起 backend/前端（编排与会话数据留在设备 B，D2 隐私边界）。
                # 不含 SearXNG——见上方 edge 分支的归属说明。
                # B2c 拆分（rag.compute.host 非空）：A 只跑无状态计算面，知识库数据在工作端；
                # 融合（host 空）：过渡拓扑，整个 RAG 仍在 A（拆分前现状）。按 config 分派。
                if [ -n "${RAGC_HOST:-}" ]; then
                    info "profile=gpu（设备 A：inference-server + RAG 计算面；知识库数据在工作端）"
                    start_rag_compute && start_infer
                else
                    warn "rag.compute.host/RAG_COMPUTE_HOST 为空——本机不启 RAG 计算面（融合模式在工作端）。拆分部署请在 .env.local 填 RAG_COMPUTE_HOST=<本机 tailnet IP>"
                    info "profile=gpu（设备 A：inference-server + RAG）"
                    start_rag && start_infer
                fi
                exit $?
                ;;
        esac
        case "$TARGET" in
            # SearXNG 为可选层：起不来只告警不阻断（web_research 会优雅不注册），故不并入 && 链
            # all-in-one 单机拆分（compute.host 指回环）时计算面也归 all；融合/远端计算则不启
            all)     { [ -n "${RAGC_HOST:-}" ] && [ "$(probe_host "$RAGC_HOST")" = "127.0.0.1" ] && { start_rag_compute || exit 1; }; true; } \
                     && start_rag && { start_searx || warn "SearXNG 未就绪，web_research 将不注册，继续启动其余服务"; true; } && start_backend && start_front ;;
            rag)     start_rag ;;
            rag-compute) start_rag_compute ;;
            searx)   start_searx ;;
            infer)   start_infer ;;
            backend) start_backend ;;   # 内部会校验 RAG 已就绪
            front)   start_front ;;
            *) fail "未知服务: ${TARGET}（可选 rag|rag-compute|searx|infer|backend|front|all）"; exit 1 ;;
        esac
        ;;
    stop)
        case "$PROFILE" in
            edge) stop_front; edge_down; exit 0 ;;
            # gpu 不含 searx（B3：已归 edge）。设备 A 上若有旧部署残留的 SearXNG，
            # 用 `./devctl.sh stop searx` 单停一次即可，不必为一次性迁移在此长留一行。
            # 拆分/融合两形态的 rag 服务都停（没在跑则是 no-op）
            gpu)  stop_infer; stop_rag_compute; stop_rag; exit 0 ;;
        esac
        case "$TARGET" in
            # all 停机含 infer：同机 remote 双进程（B1 验收形态）下它也是本机进程；没在跑则是 no-op
            all)     stop_front; stop_backend; stop_infer; stop_searx; stop_rag; stop_rag_compute ;;
            rag)     stop_rag ;;
            rag-compute) stop_rag_compute ;;
            searx)   stop_searx ;;
            infer)   stop_infer ;;
            backend) stop_backend ;;
            front)   stop_front ;;
            *) fail "未知服务: ${TARGET}（可选 rag|rag-compute|searx|infer|backend|front|all）"; exit 1 ;;
        esac
        ;;
    restart)
        # profile 必须透传，否则 restart --profile edge 会退化成 all-in-one 重启
        if [ -n "$PROFILE" ]; then
            "$0" stop --profile "$PROFILE" && "$0" start --profile "$PROFILE"
        else
            "$0" stop "$TARGET" && "$0" start "$TARGET"
        fi
        exit $?
        ;;
    bundle-print)
        # deck 打印页自托管包:构建前端并把产物放进 backend/static/webapp——
        # 前端服务不跑时,后端也能独立导出 deck PDF(pdf_export 自动回退到这份)。
        ( cd "$ROOT/frontend" && npm run build ) || { fail "前端构建失败"; exit 1; }
        rm -rf "$ROOT/backend/static/webapp"
        mkdir -p "$ROOT/backend/static"
        cp -R "$ROOT/frontend/dist" "$ROOT/backend/static/webapp"
        info "打印包已就位 → backend/static/webapp(重启 backend 生效)"
        ;;
    status) do_status ;;
    logs)   do_logs "$TARGET" ;;
    *)
        echo "用法: $0 setup                                   # 首次部署（venv/依赖/node_modules，末尾自动 keygen）"
        echo "      $0 keygen                                  # 生成/补全四把强随机 Bearer 密钥（写 .env.local，600）"
        echo "      $0 model                                   # 从注册表选择启动模型（钉死 revision）"
        echo "      $0 rag-model                                # 从 rag.tiers 选择 RAG 档位（写 .rag-tier-selection）"
        echo "      $0 start|stop|restart [rag|rag-compute|searx|infer|backend|front|all]"
        echo "      $0 start|stop|restart --profile edge   # 工作端：RAG 数据面(若本机)+backend+前端"
        echo "      $0 start|stop|restart --profile gpu    # 算力端：inference-server + RAG 计算面/融合RAG"
        echo "      $0 status"
        echo "      $0 logs [rag|rag-compute|searx|infer|backend|front]"
        echo "      $0 bundle-print                            # 构建 deck 打印包进 backend（前端不跑也能导 PDF）"
        echo "远程示例: ssh <host> '$ROOT/devctl.sh restart'"
        exit 1
        ;;
esac
