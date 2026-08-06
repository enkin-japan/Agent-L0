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
GIT_TOKEN=github_pat_xxx  # 可选：只读 fine-grained PAT；缺省则代码走本地 rsync
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

## 量化口径（为什么存 bf16 原件）

A100 是 Ampere（sm_80），无原生 FP8 张量核；vLLM 在 Ampere 上经 Marlin 内核
支持 FP8 weight-only（W8A16），且可加载 bf16 后在线量化（`--quantization fp8`），
显存约砍半（31B→~31GB）。故存原件即无悔——若某型号在线路径不支持，再补
AWQ/GPTQ/FP8 预量化版（从原件亦可自转）。

## 后续（bootstrap 波，未落地）

`bootstrap.sh`：tailscale userspace 入网（ephemeral key）→ 解包代码 → 装依赖
（Drive pip 缓存）→ 拉起 CUDA 推理服务 → 契约测试。目标冷启动 <10 分钟。
