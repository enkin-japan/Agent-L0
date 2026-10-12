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
#   ./devctl.sh start -p edge|gpu [-h <算力端IP/本机IP>]  # 分离部署（-h 一次落盘 .env.local 免重复输入）
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
# 布局形态自报（#85）：backend 的 /health 原样回显，探针据此认出「这是 devctl 起的 dev
# 后端（密钥来源 .env.local）」而非 app 的 bundle 后端——401 事故里两者被混为一谈。
export AGENT_LAYOUT=repo
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
    # 钩子在 setup 装一次：.git/hooks 不入库，新克隆天生没有它——不在这里自动装，
    # 「装执行器」这件事本身就会退化成「写下来了但没人跑」。
    install_git_hooks || warn "pre-commit 钩子未装成（可稍后单跑: ./devctl.sh install-hooks）"
    info "setup 完成。启动: ./devctl.sh start"
}

# ---- 密钥生成（keygen）----
# 四把 Bearer 钥匙（AGENT/INFER/RAG/RAG_COMPUTE）的空值/弱值（长度<16，含占位 "test"）
# 一键换成强随机串——护栏规则「非回环绑定 + 密钥缺失或过弱 = 拒启」的配套逃生口。
# 目标文件默认仓库根 .env.local，可用 DEVCTL_ENV_FILE 覆盖（测试指临时文件，绝不碰真身）。
# upsert：只改这四个键、保留文件其余行原样；已是强值则原样保留（幂等）。chmod 600。
# --rotate：强值也强制换新（密钥疑似暴露后的轮换口，2026-08-10 案）。
# 默认只打指纹（sha256 前 8）——明文进终端就可能进对话/日志（本案值泄露的通道），
# 抄值到另一侧用 --show 显式要。
do_keygen() {
    local rotate=0 show=0 arg
    for arg in "$@"; do
        case "$arg" in
            --rotate) rotate=1 ;;
            --show)   show=1 ;;
            *) fail "keygen 未知参数: ${arg}（可选 --rotate | --show）"; return 1 ;;
        esac
    done
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
        _keygen_upsert "$target" "$key" "$rotate"
    done
    chmod 600 "$target" 2>/dev/null || true

    # 指引块：四键指纹（--show 才打明文）+ 两条部署提示。
    echo ""
    if [ "$show" -eq 1 ]; then
        info "密钥就绪（${target}，权限 600）。当前生效值："
        for key in AGENT_API_KEY INFER_API_KEY RAG_API_KEY RAG_COMPUTE_API_KEY; do
            printf '    %-20s = %s\n' "$key" "$(_keygen_read "$target" "$key")"
        done
    else
        info "密钥就绪（${target}，权限 600）。指纹（sha256 前 8；抄值到另一侧用 keygen --show）："
        for key in AGENT_API_KEY INFER_API_KEY RAG_API_KEY RAG_COMPUTE_API_KEY; do
            printf '    %-20s = %s\n' "$key" "$(_keygen_fp "$target" "$key")"
        done
    fi
    echo "  部署提示："
    echo "    • 算力端与工作端两侧 .env.local 四键必须同值（Bearer 两端对齐才能互认）。"
    echo "    • macOS/iOS app 在设置页填 INFER / RAG 两把密钥；后端地址始终是工作端自己的"
    echo "      http://127.0.0.1:14159（不是算力端端口）。"
    echo "    • 算力端 .env.local 还需填 INFER_REMOTE_HOST / RAG_COMPUTE_HOST=本机 tailnet IP"
    echo "      （config 两个 host 缺省为空，通用模板不钉机器；分离部署必填。"
    echo "        也可启动时 start -p gpu -h <本机IP> 一次落盘，免手工编辑）。"
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

# 单键指纹：sha256 前 8（值不进终端；空值显示 "(空)"）。
_keygen_fp() {  # $1: 文件  $2: 键名
    local v
    v="$(_keygen_read "$1" "$2")"
    [ -n "$v" ] || { printf '(空)'; return 0; }
    printf '%s' "$v" | shasum -a 256 | cut -c1-8
}

# 空值/长度<16 → 生成强随机替换（upsert，保留其余行）；已达强度则原样保留。
# $3=1（--rotate）：强值也强制换新。
_keygen_upsert() {  # $1: 文件  $2: 键名  $3: 强制轮换（0/1，缺省 0）
    local file="$1" key="$2" force="${3:-0}" cur
    cur="$(_keygen_read "$file" "$key")"
    if [ "$force" != 1 ] && [ "${#cur}" -ge 16 ]; then
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

# -h/--host 的落盘（与 keygen 同形态）：两键 upsert 进 .env.local（600），一次录入永久生效。
# 值已在参数期过白名单（[A-Za-z0-9_.:-]），perl 替换里作字面量安全。
persist_host_env() {  # $1: 地址
    local target="${DEVCTL_ENV_FILE:-$ROOT/.env.local}" key
    [ -f "$target" ] || : > "$target"
    chmod 600 "$target" 2>/dev/null || true
    for key in INFER_REMOTE_HOST RAG_COMPUTE_HOST; do
        if grep -qE "^${key}=" "$target" 2>/dev/null; then
            /usr/bin/perl -i -pe "s|^${key}=.*|${key}=\"${1}\"|" "$target" || { fail "写入 ${target} 失败"; return 1; }
        else
            printf '%s="%s"\n' "$key" "$1" >> "$target" || { fail "写入 ${target} 失败"; return 1; }
        fi
    done
    info "算力端地址已落盘：INFER_REMOTE_HOST / RAG_COMPUTE_HOST = ${1}（${target}，后续启动免传）"
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

stop_services() {
    local service result=0
    for service in "$@"; do
        "$service" || result=1
    done
    return "$result"
}
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

# =======================================================
# check — 验证物的执行器（波 A0 ≡ 台账 F3）
#
# 病根：本仓有大量验证物（金测试、三端夹具、RAG 棘轮），但**没有任何东西会去执行
# 它们**——棘轮刚上电就已经处于「退出码没人读」的状态。本子命令只做一件事：把今天
# 要手敲的三条命令聚合起来，并把它们的退出码按纪律合并。
#
# 三值退出码纪律（整个设计里唯一不可省的语义）：
#   0 = 全过 ／ 3 = 有门判否 ／ 2 = 用法错 ／ 1 = 有门跑不起来（内部错）
# **「门跑不起来」永远不能塌缩成「门说是」。** 任何一门的前置缺失（venv / npm /
# swiftc 不在）一律记 1——自动跳过就是守卫自己把自己关掉。要跳只能人显式 --skip，
# 且跳了会在汇总里逐条打出来。
#
# 合并优先级 2 > 1 > 3 > 0：用法错时全部结果无意义；「跑不起来」意味着这套门整体
# 不可信，比「某条测试红了」更需要人介入（后者在日志正文里本来就看得见）。
#
# 反空守卫：`--only` 解析后若一门都不剩、或 `--skip` 把三门全跳掉，一律 rc=2 而非 0
# ——「一门都没跑」被记成「过」正是本仓反复得的那个病。
#
# 第一版**刻意不加任何新校验器**，只做聚合与退出码合并。加校验器是另一件事，混在
# 一起会让这条也变成「写下来了但没人跑」。
# =======================================================
# 曾有第四道门 docs（doc-numbers 防漂移，2026-08-18 并入）：2026-08-29 随台账冻结
# 归档一起退役——它守的是活跃开发期的文档数字漂移，冻结档案无漂移可防。
# 脚本与测试存档于 docs/history/tools/；复役步骤见该台账头部冻结注记。
CHECK_GATES="backend web apple"

# ---- 门的子进程不继承密钥（首审 #14）----
# 把门接到提交路径上 = 每次 `git commit` 都会把 `.env.local` 里的东西经本脚本顶部的
# `set -a` 交给 pytest / npm / xcrun 的子进程；`npm run test` 还会连带执行
# node_modules 带来的任何 lifecycle 脚本。实读 `.env.local` 的键名：五把 Bearer 钥匙
# （AGENT / INFER / RAG / RAG_COMPUTE / CLOUD_OPENAI）+ 两个 App Store Connect 标识。
# 三条门**一把都不需要**（backend 金测试是 FakeEngine、前端是夹具回放、apple 是 Swift
# 编译），故在门的子 shell 里逐个摘掉。这是本次改动**新引入**的暴露面——虽无已知泄漏
# 路径，但无声扩大暴露面本身就不该默认发生。
# 键名从 env 文件现读（不另立一张会漂移的清单）；运行必需项走安全名单排除。
CHECK_ENV_KEEP=" PATH HOME LANG LC_ALL SHELL TMPDIR TMP USER LOGNAME TERM PWD PYTHONUNBUFFERED DEVELOPER_DIR SDKROOT "
CHECK_SECRET_KEYS=""

check_collect_secret_keys() {
    local f k out=""
    for f in "$ROOT/.env" "$ROOT/.env.local"; do
        [ -f "$f" ] || continue
        for k in $(sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$f" | sort -u); do
            case "$CHECK_ENV_KEEP" in
                *" ${k} "*) ;;
                *) case " $out " in *" ${k} "*) ;; *) out="${out} ${k}" ;; esac ;;
            esac
        done
    done
    echo "$out"
}

# 在门的子 shell 里调用：把上面收集到的键逐个 unset。
check_drop_secrets() {
    local k
    for k in $CHECK_SECRET_KEYS; do unset "$k"; done
}

# 退出码合并（2 > 1 > 3 > 0）。
# ⚠️ 认不出的码**必须归 1，不能归 0**：写成「都不匹配就 echo 0」时，门函数返回 127
# （函数名拼错、不存在）会让汇总打「跑不起来」而退出码是 0——汇总说不可信、退出码说过，
# 正是本仓那个病的教科书形态。
check_merge() {
    local a="$1" b="$2" want x
    for x in "$a" "$b"; do
        case "$x" in 0|1|2|3) ;; *) echo 1; return 0 ;; esac
    done
    for want in 2 1 3; do
        if [ "$a" = "$want" ] || [ "$b" = "$want" ]; then echo "$want"; return 0; fi
    done
    echo 0
}

# backend 金测试（FakeEngine、CPU-only，不碰 GPU，可与用户日常使用并行）。
# pytest 自己的退出码本就分语义，照搬即可：0=过 1=测试红 2=中断 3=内部错 4=用法错
# 5=一条都没收集到。**5 必须记内部错**——「一条都没跑」记成过就是空守卫。
check_gate_backend() {
    local py="$BACKEND_DIR/.venv/bin/python" rc
    if [ ! -x "$py" ]; then
        fail "backend 门跑不起来：找不到 ${py}（真身在 backend/.venv；仓库根自 2026-08-18 起不再有 .venv）。先跑 ./devctl.sh setup"
        return 1
    fi
    # `( cd X && cmd )` 是个陷阱形态：cd 失败时 && 短路，子 shell 回 0 = 「门说过」。
    # 一律写成 `cd X || exit 97`，让 cd 失败落进「跑不起来」而不是「过」或「不过」。
    # 97 不与 pytest 的 0–5 冲突，故可当哨兵。
    ( check_drop_secrets; cd "$BACKEND_DIR" || exit 97; PYTHONUNBUFFERED=1 "$py" -m pytest tests -q )
    rc=$?
    case "$rc" in
        0) return 0 ;;
        1) return 3 ;;
        5) fail "backend 门跑不起来：pytest 一条测试都没收集到（rc=5）——这不是「过」"; return 1 ;;
        97) fail "backend 门跑不起来：进不去 ${BACKEND_DIR}"; return 1 ;;
        *) fail "backend 门跑不起来：pytest rc=${rc}（2=中断 3=内部错 4=用法错）"; return 1 ;;
    esac
}

# 前端协议夹具回放（vitest；与 apple 侧消费同一批 protocol/fixtures）。
# 夹具为空的空跑由 turn-reducer.test.ts 里的「发现夹具」一条自己拦（已在测），
# 此处不重造。残留口径：node_modules 齐备但 vitest 二进制损坏时 npm 也回 1，
# 会被记成「不过」而非「跑不起来」——方向安全（不塌缩成过），故不为它加脆弱的输出解析。
check_gate_web() {
    local rc
    command -v npm >/dev/null 2>&1 || { fail "web 门跑不起来：PATH 里没有 npm（PATH=${PATH}）"; return 1; }
    [ -d "$FRONT_DIR/node_modules" ] || { fail "web 门跑不起来：缺 frontend/node_modules。先跑 ./devctl.sh setup"; return 1; }
    # 97 哨兵在这一侧的依据弱于 backend：npm **会原样透传被执行脚本的退出码**，
    # 理论上 package.json 的 test 脚本可以自己回 97 而被误读成「进不去目录」。
    # 本仓的 test 脚本是 `vitest run`（0/1），故今天不冲突——如实记，不假装无风险。
    ( check_drop_secrets; cd "$FRONT_DIR" || exit 97; npm run test )
    rc=$?
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -eq 97 ] && { fail "web 门跑不起来：进不去 ${FRONT_DIR}"; return 1; }
    return 3
}

# apple-app 归约器夹具回放（Swift）。夹具目录为空由 reducer_fixtures_test.swift 自己
# 拦（`guard !names.isEmpty`），此处不重造。
# 没装 Xcode/CLT **不是「跳过」而是「跑不起来」**——想跳必须人显式 --skip apple。
check_gate_apple() {
    local sh="$ROOT/apple-app/Tests/run_reducer_fixtures.sh" rc
    [ -x "$sh" ] || { fail "apple 门跑不起来：找不到可执行的 ${sh}"; return 1; }
    command -v xcrun >/dev/null 2>&1 || { fail "apple 门跑不起来：PATH 里没有 xcrun（非 macOS 或未装 Command Line Tools）。要跳过须显式 --skip apple"; return 1; }
    if ! xcrun --find swiftc >/dev/null 2>&1; then
        fail "apple 门跑不起来：xcrun 找不到 swiftc（装 Xcode / CLT，或 DEVELOPER_DIR 指向可用的 Xcode）。要跳过须显式 --skip apple"
        return 1
    fi
    ( check_drop_secrets; exec "$sh" )
    rc=$?
    [ "$rc" -eq 0 ] && return 0
    return 3
}

check_usage() {
    echo "用法: $0 check [--only <门>] [--skip <门>]"
    echo "      门 = ${CHECK_GATES}（逗号或空格分隔，可重复给）"
    echo "      退出码: 0=全过 / 3=有门判否 / 2=用法错 / 1=有门跑不起来"
    echo "      --skip 是人的显式决定，会在汇总里逐条打出来；缺前置**不会**自动跳过"
}

do_check() {
    local skip_list="" only_list="" g rc worst=0 summary="" t0 t1 ran=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --skip)   [ $# -ge 2 ] || { fail "check: --skip 缺少参数"; check_usage >&2; return 2; }
                      skip_list="${skip_list} $(echo "$2" | tr ',' ' ')"; shift 2 ;;
            --skip=*) skip_list="${skip_list} $(echo "${1#--skip=}" | tr ',' ' ')"; shift ;;
            --only)   [ $# -ge 2 ] || { fail "check: --only 缺少参数"; check_usage >&2; return 2; }
                      only_list="${only_list} $(echo "$2" | tr ',' ' ')"; shift 2 ;;
            --only=*) only_list="${only_list} $(echo "${1#--only=}" | tr ',' ' ')"; shift ;;
            --help) check_usage; return 0 ;;
            # 本脚本全局约定「-h 表 host 不表 help」，外层参数期会把它吃成 HOST_ARG
            # （值为空，故那道「只对 start|restart 有意义」的拦截并不会触发——首审揪出
            # 我原先的注释在这里断言反了）。而 `-h` 是所有人对 help 的第一反应，
            # 故在这里显式给出指引，而不是落进「未知参数」。
            -h) fail "check: 本脚本里 -h 表 host 不表 help；要帮助请用 --help"; return 2 ;;
            *) fail "check: 未知参数 ${1}"; check_usage >&2; return 2 ;;
        esac
    done

    # 门名打错必须当场报用法错。放过它 = `--only bakend` 一门都不跑却回 0。
    for g in $skip_list $only_list; do
        case " ${CHECK_GATES} " in
            *" ${g} "*) ;;
            *) fail "check: 未知的门名 '${g}'（可选：${CHECK_GATES}）"; return 2 ;;
        esac
    done

    CHECK_SECRET_KEYS="$(check_collect_secret_keys)"
    t0=$(date +%s)
    info "check：三值退出码（0=过 / 3=不过 / 2=用法错 / 1=跑不起来）"
    for g in $CHECK_GATES; do
        if [ -n "$only_list" ]; then
            case " ${only_list} " in *" ${g} "*) ;; *) continue ;; esac
        fi
        case " ${skip_list} " in
            *" ${g} "*)
                warn "▷ ${g}：显式跳过（--skip）——这一门本次没有判定任何东西"
                summary="${summary}
  ${g}\t跳过(--skip)"
                continue ;;
        esac
        info "▷ ${g} …"
        "check_gate_${g}"
        rc=$?
        ran=$((ran + 1))
        # 门只允许回 0/3；其余（含函数不存在的 127）一律归「跑不起来」。
        # 这一行与 check_merge 里的兜底互为双保险，两处都不能省。
        case "$rc" in 0|3) ;; *) rc=1 ;; esac
        case "$rc" in
            0) summary="${summary}
  ${g}\t过" ;;
            3) summary="${summary}
  ${g}\t不过" ;;
            *) summary="${summary}
  ${g}\t跑不起来" ;;
        esac
        worst=$(check_merge "$worst" "$rc")
    done
    t1=$(date +%s)

    # 反空守卫：一门都没真跑（--only 全被跳 / --skip 跳光）不是「过」。
    if [ "$ran" -eq 0 ]; then
        fail "check: 本次一门都没有真跑（--only/--skip 把门全排除了）——这不是一次通过"
        return 2
    fi

    printf '%b\n' "${GREEN}[devctl]${NC} ── check 汇总（$((t1 - t0))s）──${summary}"
    case "$worst" in
        0) info "check 全过 ✅（rc=0）" ;;
        3) fail "check 不过 ❌（rc=3）——有门判否" ;;
        # 构造上不可达（用法错在进入本循环前就 return 2 了）；留着是防守：
        # 万一将来有门返回 2，它得有个说法，而不是掉进下面的「跑不起来」。
        2) fail "check 用法错（rc=2）" ;;
        *) fail "check 不可信 ⚠️（rc=1）——有门跑不起来；这不是「过」" ;;
    esac
    return "$worst"
}

# ---- eval-rag：RAG 检索棘轮（波 A0 ③）----
# **刻意不进 pre-commit**：它要加载检索模型、占 GPU 数分钟，塞进提交路径必然被绕过，
# 而绕过一次就会永远绕过。台账写死的纪律是：**任何检索侧改动后必须手动跑它**。
# 退出码**原样透传** run_retrieval_eval.py（其自身口径：0=不低于地板且无逐题由过转败；
# 1=判为回归/档位解析不出/拒绝把回归钉成新基线；argparse 用法错为 2）。不重映射成
# check 的三值——同一件事两套口径比一套含糊更危险。
# ⚠️ 如实声明两条口径缺陷（首审 #12，不修但不假装没有）：
#   ① **rc=1 是二义的**——「检索判为回归」与「devctl 侧前置缺失」都回 1，调用方分不开。
#      要分开只能读 stderr 文案（前者由 python 打，后者由本函数以 `[devctl]` 前缀打）。
#   ② 97 被本函数当作「进不去 rag 目录」的哨兵。实读 run_retrieval_eval.py 的退出码
#      空间是 {0,1,2}，故今天不冲突；下游若哪天真回 97 会被误读成「跑不起来」。
do_eval_rag() {
    local py="$RAG_DIR/.venv/bin/python" script="$ROOT/rag/evals/run_retrieval_eval.py" rc a
    # ⚠️ 帮助必须就地拦下，不能透传给下游 argparse：它打完帮助 exit 0，而下面的
    # `[ "$rc" -eq 0 ]` 会把这个 0 翻译成「eval-rag 过」——**一次从未跑过的 RAG 棘轮
    # 被判绿**。这与 A3 首审抓到的 doc-numbers 那条是同一个缺陷，只是它在 A0 就写下了、
    # 且当时过了首审也过了主会话签核；直到把全部子命令的 --help 拉齐排查才现形。
    # 教训：凡「透传退出码给下游」的封装，都要先问「下游有没有 exit 0 却什么都没做的路径」。
    for a in "$@"; do
        case "$a" in
            --help) echo "用法: $0 eval-rag [run_retrieval_eval.py 的参数]"
                    echo "      跑 RAG 检索棘轮并**原样透传**它的退出码（0=不低于地板 / 1=判为回归等）。"
                    echo "      ⚠️ 要加载检索模型、占 GPU 数分钟，故刻意不进 pre-commit。"
                    echo "      纪律：任何检索侧改动后必须手动跑它。"
                    echo "      下游自身的参数说明： $py $script --help"
                    return 0 ;;
            -h) fail "eval-rag: 本脚本里 -h 表 host 不表 help；要帮助请用 --help"; return 2 ;;
        esac
    done
    # 目录判在解释器判**之前**：反过来写的话，`-x .venv/bin/python` 成立已蕴含目录存在，
    # 目录那条就成了恒真的死分支（首审 #11）。
    [ -d "$RAG_DIR" ] || { fail "eval-rag 跑不起来：找不到 ${RAG_DIR}"; return 1; }
    [ -x "$py" ] || { fail "eval-rag 跑不起来：找不到 ${py}。先跑 ./devctl.sh setup"; return 1; }
    [ -f "$script" ] || { fail "eval-rag 跑不起来：找不到 ${script}"; return 1; }
    warn "RAG 棘轮要加载检索模型（占 GPU 数分钟）——单 GPU 纪律：先确认此刻没有别的真模型进程在跑"
    # 同样避开 `( cd X && cmd )`：cd 失败会让子 shell 回 0，把「压根没跑」报成「棘轮过」。
    ( cd "$RAG_DIR" || exit 97; "$py" "$script" "$@" )
    rc=$?
    [ "$rc" -eq 97 ] && { fail "eval-rag 跑不起来：进不去 ${RAG_DIR}"; return 1; }
    if [ "$rc" -eq 0 ]; then
        info "eval-rag 过（rc=0）"
    else
        fail "eval-rag 不过（rc=${rc}，原样透传 run_retrieval_eval.py）"
    fi
    return "$rc"
}

# =======================================================
# preflight — 开工事实包（波 A1）
#
# 用途：`CLAUDE.md` 的「Delegation contract」要求每次委托都写清环境事实。人手抄就会
# 抄错——实证：那条规则一直写着「仓库根 `.venv` 是死符号链接」，而 2026-08-18 仓库根
# **根本没有 `.venv`**。本子命令把事实一次性打成 JSON，委托时整段粘贴。
#
# 四条硬纪律（缺一条这份事实包就会骗人）：
#
# ① **只转述，不重写既有的门。** 端口三态由 `managed_or_report` 判、密钥由
#    `probe_auth` 判——这两道在 `start` 时刻已硬执行。preflight 复用同两个函数。
#
# ② **绝不说出自己其实不知道的事。** `probe_auth` 只在 401/403 时返回 1；**不可达、
#    连接被重置、404/500、以及权重还在加载中，它一律返回 0**（它自陈「可达性由
#    is_ready 门负责，这里只裁决鉴权」）。所以 rc=0 只能读作「这道门没判否」，
#    **绝不能写成「钥匙对」**。本函数因此根本不输出 `ok` 这个词，只输出
#    `not_rejected`——首审 Critical 1：原先只堵了「没监听」一种情形，其余全漏成 ok。
#
# ③ **GPU 占用不许按 pidfile 取数。** `bench_mlx_tile.py` 这类脚本在进程内加载 MLX，
#    既不占端口也没有 pidfile。故只认正信号（:14161 / :14967 有监听、**存活的**
#    campaign.pid），**没有正信号一律 `unknown` 而非 `free`**。
#    campaign.pid 必须 `kill -0` 验活：仓库里就躺着一个已结束战役的残留。
#
# ④ **事实流必须自证完整。** 采集函数末尾打一行 `meta complete true` 哨兵；拼装侧
#    没见到它就报「跑不起来」。否则采集半途死掉（`set -u` 下任一未定义变量即可）
#    会拼出一份只有半截 facts 的 `status: ready`——首审 Critical 2 实证。
#
# 值的格式：**`TOKEN：中文说明`**。拼装侧只认冒号前那个 ASCII token，中文随便改
# 都不会让判据失效（首审 #9：原先靠中文前缀判阻断，改个措辞就会把阻断降级成通过）。
#
# 密钥面：只报「在不在」与「那道门判没判否」，**绝不输出值、指纹、或任何完整命令行**。
# =======================================================
PF_SENTINEL_KEY="complete"

pf_emit() {  # 段 键 值 类型——统一在这里洗掉制表符与换行，保住 TSV 契约
    printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$(printf '%s' "$3" | tr '\t\n' '  ')" "$4"
}

# 本机是不是该跑这个服务：remote 形态下 host 指向别的机器，本机没监听是**正常**的，
# 不能报 free（那会让人以为「端口空着，可以起」）。
pf_is_local() {  # $1: host
    case "$(probe_host "${1:-}")" in 127.0.0.1|localhost) return 0 ;; *) return 1 ;; esac
}

pf_port_state() {  # $1: 端口  $2: pidfile  $3: stop 目标名  $4: 该服务的 host（可空=本机）
    local owners
    owners="$(listen_pids "$1")"
    if [ -z "$owners" ]; then
        if [ -n "${4:-}" ] && ! pf_is_local "$4"; then
            echo "not_local：服务按配置在远端（${4}），本机无监听是正常的"
        else
            echo "free：本机无监听"
        fi
        return
    fi
    # 复用 start 时刻那道门本身（stderr 是给人看的诊断，这里丢掉）
    if managed_or_report "preflight" "$1" "$2" "$3" >/dev/null 2>&1; then
        echo "managed：本脚本起的"
    else
        echo "foreign：被本脚本管不着的进程占用，start 会硬失败"
    fi
}

pf_gpu() {  # 打印 "状态<TAB>证据"
    local ev="" pids campaign cpid
    pids="$(listen_pids "$INFER_PORT")"
    [ -n "$pids" ] && ev="inference-server 在 :${INFER_PORT} 监听"
    # :14967 是 RAG 检索评估服务，它会把嵌入/重排模型装进 GPU——同样是正信号。
    # 端口做成可覆盖，金测试才能与宿主真栈彻底隔离（首审 Critical 3：`listen_pids`
    # 是 `lsof -ti tcp:<port>`，全局作用于宿主，与临时 ROOT 无关；不隔离的话
    # dev 栈一起来这些用例就红，而它们在 pre-commit 里 → **卡死全部提交**）。
    [ -n "$(listen_pids "${RAG_EVAL_PORT:-14967}")" ] && ev="${ev}${ev:+；}RAG 评估服务在 :${RAG_EVAL_PORT:-14967} 监听"
    for campaign in "$ROOT"/benchmarks/baseline/*/campaign.pid; do
        [ -f "$campaign" ] || continue
        cpid="$(cat "$campaign" 2>/dev/null)"
        # ⚠️ 必须验活：已结束战役会留下 campaign.pid，光看文件在不在会反向误报 busy
        [ -n "$cpid" ] && kill -0 "$cpid" 2>/dev/null || continue
        ev="${ev}${ev:+；}战役 pid ${cpid} 存活"
    done
    if [ -n "$ev" ]; then
        printf 'busy\t%s\n' "$ev"
    else
        printf 'unknown\t%s\n' "无正信号——但这**推不出空闲**：bench_mlx_tile.py 这类脚本在进程内加载 MLX，既不占端口也无 pidfile。开跑真模型前须人工确认"
    fi
}

# 参考性进程扫描：**只打 pid 与可执行名，绝不打完整命令行**——命令行是无界文本，
# 可能带上 --api-key/--hf-token 之类，而这份 JSON 的用途正是被粘进对话（首审 Critical 4）。
# 也不用来把 unknown 升格成 free。
pf_model_procs() {
    local p out=""
    for p in $(pgrep -f 'bench_.*\.py|inference/server\.py|mlx_lm|mlx_vlm|soak_longrun' 2>/dev/null); do
        [ "$p" = "$$" ] && continue
        out="${out}${out:+ }${p}:$(ps -o comm= -p "$p" 2>/dev/null | sed 's|.*/||')"
    done
    echo "$out"
}

preflight_facts() {
    # 全部 local：`key` / `v` 持有密钥值，绝不让它们在函数返回后留在全局。
    local py ver st ev gpu_line pair k v name port url key host dirty
    local infer_host="${INFER_HOST:-}" rag_host="${RAG_HOST:-}"

    pf_emit repo root "$ROOT" str
    pf_emit repo branch "$(cd "$ROOT" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)" str
    pf_emit repo head "$(cd "$ROOT" && git rev-parse --short HEAD 2>/dev/null || echo unknown)" str
    # dirty 是唯一一个「问不出来」会被答成「没问题」的字段：git 不在/不是仓库时
    # `[ -n "$(...)" ]` 恒假 → false → 读成「干净」。必须分出 unknown（首审 #5）。
    if ! (cd "$ROOT" && git rev-parse --git-dir >/dev/null 2>&1); then
        dirty="unknown：这里不是 git 仓库或 git 不可用"
    elif [ -n "$(cd "$ROOT" && git status --porcelain 2>/dev/null)" ]; then
        dirty="true：工作树有未提交改动"
    else
        dirty="false：工作树干净"
    fi
    pf_emit repo dirty "$dirty" str

    for pair in "backend:$BACKEND_DIR" "rag:$RAG_DIR"; do
        py="${pair#*:}/.venv/bin/python"
        if [ -x "$py" ]; then
            ver="$("$py" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || echo unknown)"
            pf_emit venv "${pair%%:*}" "ok：${py} (python ${ver})" str
        else
            pf_emit venv "${pair%%:*}" "missing：${py} 不存在，先跑 ./devctl.sh setup" str
        fi
    done
    # 仓库根的 .venv：这条曾被手抄进 CLAUDE.md 当作「死符号链接」，而实测早已不是。
    # 现在一律现算，不写死任何结论。
    if [ -L "$ROOT/.venv" ] && [ ! -e "$ROOT/.venv" ]; then
        pf_emit venv repo_root "dangling：死符号链接，**不要用**；唯一真身是 backend/.venv" str
    elif [ -e "$ROOT/.venv" ]; then
        pf_emit venv repo_root "present：存在（用前先确认它指向哪）" str
    else
        pf_emit venv repo_root "absent：不存在" str
    fi
    pf_emit venv test_cmd "cd backend && PYTHONUNBUFFERED=1 .venv/bin/python -m pytest tests -q" str

    pf_emit ports "backend_${BACKEND_PORT}" "$(pf_port_state "$BACKEND_PORT" "$LOG_DIR/backend.pid" backend "")" str
    pf_emit ports "rag_${RAG_PORT}" "$(pf_port_state "$RAG_PORT" "$LOG_DIR/rag.pid" rag "$rag_host")" str
    pf_emit ports "infer_${INFER_PORT}" "$(pf_port_state "$INFER_PORT" "$LOG_DIR/infer.pid" infer "$([ "${INFER_MODE:-}" = remote ] && echo "$infer_host")")" str
    pf_emit ports "searx_${SEARX_PORT}" "$(pf_port_state "$SEARX_PORT" "$LOG_DIR/searx.pid" searx "")" str
    pf_emit ports "frontend_${FRONT_PORT}" "$(pf_port_state "$FRONT_PORT" "$LOG_DIR/front.pid" front "")" str
    pf_emit ports "rag_eval_${RAG_EVAL_PORT:-14967}" "$([ -n "$(listen_pids "${RAG_EVAL_PORT:-14967}")" ] && echo 'busy：RAG 评估服务在跑（占 GPU）' || echo 'free：本机无监听')" str

    gpu_line="$(pf_gpu)"
    pf_emit gpu status "${gpu_line%%	*}" str
    pf_emit gpu evidence "${gpu_line#*	}" str
    pf_emit gpu process_scan "$(pf_model_procs)" str
    pf_emit gpu rule "单 GPU 无抢占：任一时刻最多一个真模型进程（bench / :14161 / devctl 真模型测试三者互斥）" str

    for k in AGENT_API_KEY INFER_API_KEY RAG_API_KEY RAG_COMPUTE_API_KEY; do
        eval "v=\${$k:-}"
        pf_emit keys "$k" "$([ -n "${v:-}" ] && echo 'present：已配置' || echo 'absent：未配置')" str
    done
    # ⚠️ 见纪律 ②：这里给出的**永远不是「钥匙对」**，只可能是「那道门没判否」。
    # 钥匙不参与任何字符串手术：按名字取，不拼进待切分的串（URL 与钥匙都可能含冒号）。
    for name in INFER RAG; do
        eval "port=\${${name}_PORT:-}"
        eval "url=\${${name}_AUTH_URL:-}"
        eval "key=\${${name}_API_KEY:-}"
        eval "host=\${${name}_HOST:-}"
        if [ -z "${key:-}" ]; then
            st="no_key：未配置钥匙，探测无意义"
        elif [ -z "$(listen_pids "${port:-0}")" ] && pf_is_local "${host:-}"; then
            st="not_running：本机没监听、服务也不在远端，测不了鉴权（不报 ok）"
        elif probe_auth "$name" "${url:-}" "$key" >/dev/null 2>&1; then
            st="not_rejected：那道门没判否。**这不等于钥匙对**——probe_auth 对不可达、连接重置、404/500 也返回 0"
        else
            st="FAIL：401/403，start 会当场拒启"
        fi
        pf_emit keys "${name}_verified" "$st" str
    done
    pf_emit keys manual_only "AGENT_API_KEY 与 RAG_COMPUTE_API_KEY 无自动比对（INFER/RAG 两把 start 时由 probe_auth 真认证）" str

    pf_emit selection model "$([ -f "$ROOT/.model-selection" ] && tr -d '\n' < "$ROOT/.model-selection" || echo 'unselected：未选，回落 config.yaml 缺省')" str
    pf_emit selection rag_tier "$([ -f "$ROOT/.rag-tier-selection" ] && sed -n 's/.*"tier"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$ROOT/.rag-tier-selection" 2>/dev/null | head -1 || echo 'unselected：未选，回落 config.yaml rag.active_tier')" str
    pf_emit selection inference_mode "${INFER_MODE:-unknown}" str
    # remote 形态而地址为空 = start 必被 require_host 拒（新克隆的 config 就是空模板）
    if [ "${INFER_MODE:-}" = remote ] && [ -z "$(printf '%s' "${INFER_HOST:-}" | tr -d '[:space:]')" ]; then
        pf_emit selection infer_host "empty_but_remote：mode=remote 而地址为空，start 会被 require_host 拒" str
    else
        pf_emit selection infer_host "ok：${INFER_HOST:-（本机）}" str
    fi

    pf_emit policy real_model_execution "默认不允许——委托里要显式授予才可跑" str
    pf_emit policy safe_concurrently "backend/tests（FakeEngine、CPU-only）与 ./devctl.sh check 随时可跑" str

    # ④ 完整性哨兵：必须是最后一行。采集半途死掉就见不到它。
    pf_emit meta "$PF_SENTINEL_KEY" true bool
}

do_preflight() {
    local a rc
    for a in "$@"; do
        case "$a" in
            --help) echo "用法: $0 preflight"
                    echo "      打印开工事实包 JSON（委托时整段粘进 CLAUDE.md 的 Delegation contract）。"
                    echo "      退出码: 0=可开工（可能含须人工确认项）/ 3=有阻断项 / 2=用法错 / 1=跑不起来"
                    return 0 ;;
            -h) fail "preflight: 本脚本里 -h 表 host 不表 help；要帮助请用 --help"; return 2 ;;
            *) fail "preflight: 未知参数 ${a}"; return 2 ;;
        esac
    done
    [ -f "$ROOT/scripts/preflight_json.py" ] || { fail "preflight 跑不起来：找不到 scripts/preflight_json.py"; return 1; }
    command -v python3 >/dev/null 2>&1 || { fail "preflight 跑不起来：找不到 python3（仅用于把事实拼成 JSON）"; return 1; }
    preflight_facts | python3 "$ROOT/scripts/preflight_json.py"
    rc=$?
    # python3 自身的退出码（缺模块=1、用法错=2…）会与本脚本的三值表撞车。
    # 只认 0 与 3，其余一律归「跑不起来」——绝不让 2 被读成「用法错」（首审 #15）。
    case "$rc" in
        0) ;;
        3) fail "preflight: 有阻断项（rc=3）——见 blockers" ;;
        *) fail "preflight 跑不起来（rc=${rc}）——这不是「可开工」"; rc=1 ;;
    esac
    return "$rc"
}

# ---- git 钩子安装：把 check 接到提交路径上 ----
# 钩子正文入库在 scripts/git-hooks/pre-commit（.git/hooks 不受版本控制，新克隆天生
# 没有钩子——所以正身必须入库）。.git/hooks/pre-commit 只放一个三行 shim。
#
# **刻意不用软链**：软链要么钉死绝对路径（仓库一搬家就成悬空链接），要么钉死相对层级
# （worktree/submodule 下 hooks 目录不在 <root>/.git/hooks）。而 git 对「执行不了的
# 钩子」是**静默跳过**的——那就又变成守卫自己把自己关掉。shim 每次运行现算仓库根，
# 搬家、worktree 都不受影响；正身缺失时 exec 失败回非零，方向也是挡住提交。
HOOK_SHIM_MARK="# managed-by: devctl install-hooks"

install_git_hooks() {
    local src="$ROOT/scripts/git-hooks/pre-commit" hooks_dir dest
    [ -f "$src" ] || { fail "找不到 ${src}"; return 1; }
    hooks_dir="$(cd "$ROOT" && git rev-parse --git-path hooks 2>/dev/null)" || hooks_dir=""
    # 不在 git 仓库里回 **1 不是 0**：没装成就不能说装成了（tarball 部署的宽容由调用方
    # 决定——do_setup 那边是 `|| warn`，不阻断 setup）。
    [ -n "$hooks_dir" ] || { fail "钩子未装：这里不是 git 仓库"; return 1; }
    case "$hooks_dir" in /*) ;; *) hooks_dir="$ROOT/$hooks_dir" ;; esac
    mkdir -p "$hooks_dir" || return 1
    dest="$hooks_dir/pre-commit"
    # 悬空软链（本工具早期版本装的绝对路径软链，仓库搬家后即悬空）：`-e` 跟随软链，
    # 判不出它；不先摘掉就会被下面的 `cat >` **写穿到软链目标那个仓外路径**上去，
    # 造出一个野文件、而钩子本身仍然是悬空的。
    if [ -L "$dest" ]; then
        if [ "$(readlink "$dest")" = "$src" ]; then
            rm -f "$dest"          # 是本工具装的，就地升级成 shim
        else
            warn "已存在非本工具管理的软链 ${dest} → $(readlink "$dest")——不覆盖。"
            return 1
        fi
    fi
    # 别人的钩子不许静默覆盖；自己上次装的（认标记行）可以就地更新。
    if [ -e "$dest" ] && ! grep -qF "$HOOK_SHIM_MARK" "$dest" 2>/dev/null; then
        warn "已存在非本工具管理的 ${dest}——不覆盖。备份并删掉它再重跑 ./devctl.sh install-hooks"
        return 1
    fi
    cat > "$dest" <<SHIM
#!/bin/bash
${HOOK_SHIM_MARK}
exec "\$(git rev-parse --show-toplevel)/scripts/git-hooks/pre-commit" "\$@"
SHIM
    chmod +x "$dest" || { fail "钩子安装失败：${dest}"; return 1; }
    info "pre-commit 钩子已装 → ${dest}（转调入库的 scripts/git-hooks/pre-commit）"
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

# 只列编号+模型名（用户拍板 2026-08-31）：note 注记成段拼进来会把「选了哪个」
# 淹没——说明归 config.yaml/文档，这里只回答一个问题：现在是哪个、可选哪些。
effective = current or default_repo
print("可用模型（各档说明见 config.yaml inference.model_registry 注记）：")
for i, e in enumerate(registry, 1):
    name = e["repo"].split("/")[-1]
    default_tag = "（默认）" if e["repo"] == default_repo else ""
    arrow = "  ← 当前" if e["repo"] == effective else ""
    print(f"  {i:2d}. {name}{default_tag}{arrow}")
src = "来自 .model-selection 选择" if current else "config 默认"
print(f"\n当前生效：{effective.split('/')[-1]}（{src}）")

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
# 换档 = 选择，非手填（HETERO_COMPUTE B2a）。换档纪律：库一旦入库档位即固定，换嵌入模型
# 须重建向量库——重建由**执行端**（app/数据面）驱动，devctl 侧刻意不做自动化（分离部署
# 审计 2026-08-31 用户拍板，曾做过 devctl 版当日移除）。非 TTY（脚本化）只打印状态不阻塞。
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

# 与 ./devctl.sh model 同风格（用户拍板 2026-08-31）：只列编号+档位 id+模态，
# note/阈值标定注记归 config.yaml 与 rag/README 的档位表。
effective = current or default_id
print("可用 RAG 档位（档 = 一对 嵌入+重排 模型；各档说明见 rag/README 档位表）：")
for i, t in enumerate(tiers, 1):
    default_tag = "（默认）" if t["id"] == default_id else ""
    arrow = "  ← 当前" if t["id"] == effective else ""
    print(f"  {i:2d}. {t['id']:10s} [{t.get('modality','text')}]{default_tag}{arrow}")
src = "来自 .rag-tier-selection 选择" if current else "config 默认"
print(f"\n当前生效：{effective}（{src}）")

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
PY
    local rc=$?
    [ "$rc" -eq 0 ] || return "$rc"
    # 换档纪律：库一旦入库档位即固定（chroma_data/.tier_stamp.json 护栏快速失败）。
    # 重建的正式口径 = 执行端（app/数据面服务）驱动——分离部署审计后用户拍板
    # （2026-08-31）：devctl 侧不做重建自动化，模型的事归计算端，库的事归执行端。
    warn "换档纪律：换嵌入模型须重建向量库。正式口径=执行端驱动（app 检测提示，或对数据面"
    warn "POST /api/v1/maintenance/retier/rebuild）；手动后备 rag/scripts/reembed.py。"
    warn "重启生效: ./devctl.sh restart rag"
}

# ---- 命令分发 ----
CMD="${1:-}"
shift 2>/dev/null || true

# 参数期之前留一份**原始 argv**：下面的 -p/-h 解析用 `set -- $_args` 重排位置参数，
# 那一步会做词分割与通配展开——`--reason "两个 词"` 会被打散成两个参数。服务类命令
# 的参数都是单词，不受影响；但 check / eval-rag 要把参数**原样**递给下游（eval-rag
# 更是承诺了「退出码与参数都原样透传」），必须走这份没被动过的副本。
# bash 3.2 下 `"${ARR[@]}"` 在数组为空且 set -u 时会报 unbound，故一律用 `${ARR[@]+...}`。
RAW_ARGS=("$@")

# --profile edge|gpu（HETERO_COMPUTE B3）：把 all-in-one 拆成两台设备的角色。
#   （无 profile）= 现状 all-in-one，永不破坏——不传旗标时下面的分发与改动前逐字等价。
#   edge = 设备 B（用户 Mac）：backend + 前端；推理/RAG 在设备 A（config 的 host 指过去）。
#   gpu  = 设备 A（算力机）：inference-server + RAG (+ SearXNG)；不起前端、不起编排。
PROFILE=""
HOST_ARG=""
_args=""
while [ $# -gt 0 ]; do
    case "$1" in
        # ⚠️ `shift 2 || true` 在只剩 1 个参数时**不移位**（bash：n>$# 则 shift 失败且
        # 原地不动），`|| true` 又把错吞了 —— 循环条件永远成立 = 死循环。
        # 实证：`./devctl.sh start -p` 与 `./devctl.sh check -h` 都会挂死不返回；在
        # 提交路径/CI 上等价于永久阻塞。一律改成「先移一格，还有才再移一格」。
        -p|--profile) PROFILE="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --profile=*) PROFILE="${1#--profile=}"; shift ;;
        -p=*)        PROFILE="${1#-p=}"; shift ;;
        # -h 表 host 不表 help（mysql/psql 先例；本脚本的帮助本就是「未知命令打用法」）
        -h|--host)   HOST_ARG="${2:-}"; shift; [ $# -gt 0 ] && shift ;;
        --host=*)    HOST_ARG="${1#--host=}"; shift ;;
        -h=*)        HOST_ARG="${1#-h=}"; shift ;;
        *) _args="$_args $1"; shift ;;
    esac
done
# ⚠️ 无引号展开（且没开 set -f）：这一步会做词分割**和通配展开**，
# `./devctl.sh logs '*'` 之类会被当前目录的文件名污染。既有问题，本次未引入也未修；
# check / eval-rag 已改走上面的 RAW_ARGS，不经过这里。
# shellcheck disable=SC2086
set -- $_args
TARGET="${1:-all}"
# 兼容旧服务名（拆分仓时代）：agent → backend
[ "$TARGET" = "agent" ] && TARGET="backend"

case "$PROFILE" in
    ""|edge|gpu) ;;
    *) fail "未知 profile: ${PROFILE}（可选 edge|gpu；不传 = all-in-one 现状）"; exit 1 ;;
esac

# -h/--host：算力端地址一次录入、永久落盘（.env.local，与 keygen 同形态），本次启动
# 立即生效——后续启动免传。语义与 app 设置「算力端地址」一致：同一值灌
# INFER_REMOTE_HOST + RAG_COMPUTE_HOST 两键（gpu = 本机绑定地址，edge = 算力端拨号
# 地址）。混合拓扑（两服务各在一机）或 gpu 融合形态（RAG 整体留算力端，
# RAG_COMPUTE_HOST 须留空）不走本参数——那两种形态直接在 .env.local 分别维护两键。
if [ -n "$HOST_ARG" ]; then
    case "$CMD" in
        start|restart) ;;
        *) fail "-h/--host 只对 start|restart 有意义（当前命令：${CMD:-空}）"; exit 1 ;;
    esac
    if [ -z "$PROFILE" ]; then
        fail "单机形态不需要 -h/--host（inprocess + 融合 RAG 无跨机地址）；分离部署请配合 -p edge|gpu 使用"
        exit 1
    fi
    case "$HOST_ARG" in
        -*) fail "-h/--host 缺少地址值（用法：-h 100.x.y.z）"; exit 1 ;;
        *[!A-Za-z0-9_.:-]*) fail "非法地址值：${HOST_ARG}（期望 IP/主机名）"; exit 1 ;;
    esac
    persist_host_env "$HOST_ARG" || exit 1
    export INFER_REMOTE_HOST="$HOST_ARG" RAG_COMPUTE_HOST="$HOST_ARG"
    edge_resolve_urls   # .env.local 在参数解析前已加载；覆盖后重算 HOST/URL 全套
fi

case "$CMD" in
    setup) do_setup ;;
    keygen) do_keygen "$@" ;;
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
                # 分离启动缺地址：参数期就拦（-h/.env.local/config 三层都空），不进编队半途才炸。
                # start_backend 内的 require_host 保留为第二层（app 形态走的是那条）。
                if [ "$INFER_MODE" = "remote" ]; then
                    require_host "$INFER_HOST" "算力端（inference-server 拨号地址）" \
                        "启动时传 -h <算力端 tailnet IP>（一次落盘 .env.local，后续免传），或 .env.local 写 INFER_REMOTE_HOST=<算力端 IP>；单机部署改 inference.mode: inprocess" \
                        || exit 1
                fi
                edge_up && start_front
                exit $?
                ;;
            gpu)
                # 设备 A：只起重型计算。不起 backend/前端（编排与会话数据留在设备 B，D2 隐私边界）。
                # 不含 SearXNG——见上方 edge 分支的归属说明。
                # B2c 拆分（rag.compute.host 非空）：A 只跑无状态计算面，知识库数据在工作端；
                # 融合（host 空）：过渡拓扑，整个 RAG 仍在 A（拆分前现状）。按 config 分派。
                # 分离启动缺地址：参数期就拦（-h/.env.local/config 三层都空），
                # 别等 start_infer 里的同名门——那时 rag 面可能已经起了一半。
                require_host "$INFER_HOST" "算力端（inference-server 绑定地址）" \
                    "启动时传 -h <本机 tailnet IP>（一次落盘 .env.local，后续免传），或 .env.local 写 INFER_REMOTE_HOST / RAG_COMPUTE_HOST=<本机 tailnet IP>；单机部署改 inference.mode: inprocess 并去掉 -p gpu" \
                    || exit 1
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
            edge) stop_services stop_front edge_down; exit $? ;;
            # gpu 不含 searx（B3：已归 edge）。设备 A 上若有旧部署残留的 SearXNG，
            # 用 `./devctl.sh stop searx` 单停一次即可，不必为一次性迁移在此长留一行。
            # 拆分/融合两形态的 rag 服务都停（没在跑则是 no-op）
            gpu)  stop_services stop_infer stop_rag_compute stop_rag; exit $? ;;
        esac
        case "$TARGET" in
            # all 停机含 infer：同机 remote 双进程（B1 验收形态）下它也是本机进程；没在跑则是 no-op
            all)     stop_services stop_front stop_backend stop_infer stop_searx stop_rag stop_rag_compute ;;
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
        # profile 必须透传，否则 restart --profile edge 会退化成 all-in-one 重启。
        # -h 不必透传：参数期已落盘 .env.local，子调用 source 时自然拾取（落盘即透传）。
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
    check)  do_check ${RAW_ARGS[@]+"${RAW_ARGS[@]}"}; exit $? ;;
    eval-rag) do_eval_rag ${RAW_ARGS[@]+"${RAW_ARGS[@]}"}; exit $? ;;
    preflight) do_preflight ${RAW_ARGS[@]+"${RAW_ARGS[@]}"}; exit $? ;;
    install-hooks) install_git_hooks; exit $? ;;
    *)
        echo "用法: $0 setup                                   # 首次部署（venv/依赖/node_modules，末尾自动 keygen）"
        echo "      $0 keygen [--rotate] [--show]              # 生成/补全四把强随机 Bearer 密钥（写 .env.local，600）"
        echo "                                                 #   --rotate 强值也换新（轮换）；--show 打明文（默认只打指纹）"
        echo "      $0 model                                   # 从注册表选择启动模型（钉死 revision）"
        echo "      $0 rag-model                                # 从 rag.tiers 选择 RAG 档位（写 .rag-tier-selection）"
        echo "      $0 start|stop|restart [rag|rag-compute|searx|infer|backend|front|all]"
        echo "      $0 start|stop|restart -p edge [-h <算力端IP>]   # 工作端：RAG 数据面(若本机)+backend+前端"
        echo "      $0 start|stop|restart -p gpu  [-h <本机IP>]     # 算力端：inference-server + RAG 计算面/融合RAG"
        echo "      （-p = --profile；-h/--host 一次落盘 .env.local 两键 INFER_REMOTE_HOST/RAG_COMPUTE_HOST，后续免传）"
        echo "      $0 check [--only <门>] [--skip <门>]  # 跑全部验证物（backend/web/apple 三门）"
        echo "                                                 #   退出码 0=过 3=不过 2=用法错 1=跑不起来（缺前置不会自动跳过）"
        echo "      $0 eval-rag [...]                          # RAG 检索棘轮（占 GPU 数分钟，故不进 pre-commit）"
        echo "                                                 #   ⚠️ 任何检索侧改动后必须手动跑它；退出码原样透传"
        echo "      $0 preflight                               # 开工事实包 JSON（委托时整段粘贴；0=可开工 3=有阻断）"
        echo "      $0 install-hooks                           # 装 .git/hooks/pre-commit（三行 shim，转调入库的 scripts/git-hooks/pre-commit）"
        echo "      $0 status"
        echo "      $0 logs [rag|rag-compute|searx|infer|backend|front]"
        echo "      $0 bundle-print                            # 构建 deck 打印包进 backend（前端不跑也能导 PDF）"
        echo "远程示例: ssh <host> '$ROOT/devctl.sh restart'"
        exit 1
        ;;
esac
