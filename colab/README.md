# Colab 算力端（CUDA 兼容波）

Colab A100 在拓扑里就是"设备 A"：backend/RAG/前端留在本机，Colab 只跑推理承载，
经 Tailscale 组网后 `./devctl.sh start -p edge -h <colab节点>` 走现有 remote 分离
部署形态。Drive 是跨会话的持久层（Colab 本地盘每次清零）。

## Drive 布局（MyDrive/AgentColab/）

```
AgentColab/
  secrets/colab.env      # 你手工创建；密钥绝不进仓库/对话
  code/                  # Agent-L0.bundle + Agent-L0-src.tar.gz（stage 脚本产出）
  models/<org>__<name>/  # bf16 原件，每目录一个 .complete 断点标记
  cache/                 # pip 缓存等（bootstrap 波使用）
```

`secrets/colab.env` 内容形如：

```
HF_TOKEN=hf_xxx           # gemma 系 gated 仓库必需（先在 HF 网页接受许可）
GIT_TOKEN=github_pat_xxx  # 必需（仓库为私密仓，2026-08-08 起）：只读 PAT，匿名 clone 必挂
TS_AUTHKEY=tskey-auth-xxx # Tailscale ephemeral+reusable auth key（headless 入网）
INFER_API_KEY=<≥16位>     # 推理服务鉴权钥（与本机 .env.local 同值，经管道同步不经对话）
```

## 运维纪律（实测沉淀，详见 EXECUTION_LEDGER 大档评测波）

- **权重获取优先序（A100 会话）**：HF+Xet 直下（实测 281-357MB/s）> Drive 中转
  （FUSE rsync ~45MB/s）。Xet 只在大内存 VM（83G+）解禁且挂 30s 内存哨兵；
  小内存环境（免费 T4 13G）一律 `HF_HUB_DISABLE_XET=1`（内存气球实锤）。
- **CLI 会话（google-colab-cli）**：`colab new -s <名> --gpu A100` 后**立即备份**
  `~/.config/colab-cli/sessions.json`——exec 404 时 CLI 自清本地状态会弄丢 stop 所需
  的 per-session JWT，VM 变成停不掉的孤儿；还原备份再 `colab stop` 即自救。
  `colab exec` 只吃 `-f 文件`/stdin，无位置参数。
- **多实例分层一律顺序装载**（前一实例 /health 就绪才起下一个）；重活前 `free -g`。
- **等 GPU 长任务必挂看门狗**（停滞+超时双报警）；测试性质的临时拉起用完即收。

## 一次性布置（stage_to_drive.sh）

1. 把本目录的 `stage_to_drive.sh` 拖进 Drive 的 `AgentColab/`（一次即可）；
2. Colab 开任意 GPU/CPU 实例，notebook 两个 cell：

```python
from google.colab import drive
drive.mount('/content/drive')
```

```
!bash /content/drive/MyDrive/AgentColab/stage_to_drive.sh
```

体量约 350GB（主模型 bf16 ~254GB + 向量/重排 ~90GB），Drive 需 2TB 档；
首跑 2–5 小时。**可重入**：会话中断后重跑同一 cell 即断点续传（.complete 跳过）。
免费档（CPU 机型即可，无需 GPU）本地盘仅 ~100GB：脚本安全线 75GB，不够时自动
走 Drive 直下兜底（慢路）；同会话内下载失败保留中转续传。

## 量化口径（为什么存 bf16 原件）

A100 是 Ampere（sm_80），无原生 FP8 张量核；vLLM 在 Ampere 上经 Marlin 内核
支持 FP8 weight-only（W8A16），且可加载 bf16 后在线量化（`--quantization fp8`），
显存约砍半（31B→~31GB）。故存原件即无悔——若某型号在线路径不支持，再补
AWQ/GPTQ/FP8 预量化版（从原件亦可自转）。

## 会话引导（bootstrap.sh + agent_colab.ipynb）

A100 会话（`agent_colab.ipynb` 三个 cell）：挂 Drive → `bootstrap.sh <模型repo>` →
保活心跳。引导五步（幂等可重跑）：tailscale userspace 入网（ephemeral key，入站
自动转发到本机端口→服务绑回环即可、护栏不触碰）→ 解包代码 → pip 依赖（Drive
缓存，**不用 backend/requirements.txt——那是 MLX 家的**）→ 权重 Drive→本地盘
（补 `.download_complete` 标记喂 path_resolver）→ 拉起 `INFER_BACKEND=cuda` 的
inference-server + **契约测试**（`backend/scripts/check_infer_contract.py`，18 项
协议断言，已在本地对 MLX 服务全过——它就是"换后端服务一行不动"的背书）。

colab.env 需三行：`HF_TOKEN` / `TS_AUTHKEY`（ephemeral+reusable）/
`INFER_API_KEY`（≥16 位，edge 侧 .env.local 同值）。

edge 侧收尾：`./devctl.sh start -p edge -h colab-a100`（MagicDNS 名或 100.x IP）。

## 日常代码同步（sync_to_colab.sh，本地跑）

本地改完 → `./colab/sync_to_colab.sh` → 打 HEAD 工作树 tar 进 Drive 同步文件夹
（自动探测桌面版挂载点；含未提交改动，排除 .venv/models/sessions 等重资产）。
Colab 侧取新包：`rm -rf /content/agent` 后重跑 bootstrap（幂等）。

## CUDA 后端（backend/framework/backends/cuda/）

vLLM 实现 InferenceBackend 协议（server 一行不动，`INFER_BACKEND=cuda` 起）。
诚实边界（联调清单，代码内均响亮告警）：thinking 预算裁决未实现；
repetition_context_size 无对应物；vision/audio 载荷透传未实测；per-request
logits_processors 若被所装 vLLM 版本拒绝→约束降级为上游解析兜底（structural-tag
迁移候补）；Qwen3.6 方言按 qwen35 预注册，模板兼容性=联调首验项。

## bench 专用会话（bench_bootstrap.sh + bench_mtp_recheck.sh，2026-08-15 起）

评测批不走 serve 链（server 起完就得杀掉腾 GPU，且是链条里最脆的段）：Mac 侧
`colab new` → 上传 colab.env/引导/驱动/评测件 → exec 点火 `bench_bootstrap.sh`
（tailscale+代码+pip 撕裂处置+拉件+热补丁，就绪信号 BOOT_READY）→ **之后一律走
ssh/scp（root@colab-a100）**——exec 会话凭据 ~45min 必失效而 VM 照常计费（08-15
四连崩验尸，§8.3 三案同因），12h 长稳形态的本质就是 exec 只点火。驱动
`bench_mtp_recheck.sh` 逐格出 `RESULT` 行、产物落 /content/bench_out/，Mac 侧
逐格 scp 取回（会话丢最多损失一格）。新脚本首跑按磨合纪律分钟级盯梢
（持久记忆 new-script-shakedown-monitoring）。

## 日用一键起停（daily.sh，2026-08-09 起）

白天开、晚上关的日常算力端（背景：余量 unit 榨汁，L4 1.54u/h 可支撑约 20 天日用）。

```bash
./colab/daily.sh start        # L4 档：26B NVFP4（tool 0.96/longchain 1.0；
                              #   vision hard 档 -8pp，视觉关键活换 a100 档）
./colab/daily.sh start a100   # A100 40G 档：26B fp8+MTP k=4 生产形态（5.3u/h）
./colab/daily.sh status       # 会话 / 服务健康 / 时长与预估 unit
./colab/daily.sh stop         # 停哨兵+会话；孤儿自动走还原备份自救
```

一次性前置：`mkdir -p ~/.config/agent-colab && chmod 700 ~/.config/agent-colab`，
从 Drive `AgentColab/secrets/colab.env` 拷入该目录（chmod 600）。起服就绪后本机
`./devctl.sh start all` 即连上（`.env.local` 已拨 colab-a100）。

防崩溃措施（全部来自各站实证教训）：
- `colab new` 后**立即备份 sessions.json**——CLI 404 自清会删掉 stop 所需凭据，
  孤儿 VM 靠还原备份自救（`rescue` 子命令，账本 66eec21 案）；
- VM 侧**监理循环**：server 崩了自动重启（≤5 次/天），`/content/logs/supervisor.log` 留痕；
- **GPU 保活 0.5G**（gpu_keepalive.py pidfile 纪律）防用户侧 25min 判零看门在
  空闲窗误杀；
- Mac 侧**隧道保活替身**（tunnel_keepalive.py）——CLI 自带 keep_alive 被 404 连坐
  杀死的长稳站教训；
- Mac 侧**哨兵**：2min 健康轮询（失联 10min 弹 macOS 通知）+ 20min 指标回拉
  （活跃信号）；
- 起服卡死有 30min 停滞判死；`ssh-keygen -R colab-a100` 每次 start 自动清旧钥。

旋钮（env 传给 start）：`SERVE_MAXLEN`（L4 显存紧时下调上下文上限）、
`SERVE_KV_DTYPE`（L4 档缺省 fp8——KV 减半 +20% 提速、tool_call 零漂移实测；
传 auto 退回。A100=SM80 不支持，缺省即 auto）。
