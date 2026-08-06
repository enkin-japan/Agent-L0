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
GIT_TOKEN=github_pat_xxx  # 可选：仓库公开期匿名拉取即可；转回私库时补只读 PAT
```

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
