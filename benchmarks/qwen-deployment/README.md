# Qwen 部署验证脚本

支撑 `colab/README.md` 吞吐表（:187-208）和 64K 上下文依据（:79-81）的两份脚本。两者都只验证
已在运行的服务，不加载模型、不启服务。

## `remote-validation.py`

运行位置：VM（经 `colab exec`，或在 VM 内手动 `python3 remote-validation.py`）。顺序校验正在运行的
推理引擎：核对 `model_repo`/`model_revision`、确认无并发生成、确认单一 GPU 进程、对三类合成提示
（长文写作、代码生成、12K+ token 长上下文）各跑 4 次取吞吐与 TTFT，核验 MTP 草稿/接受计数增量、工具
调用解析（qwen35 dialect）、以及 OpenAI 门面转发，并验证切换时的「忙碌拒绝」保护。密钥通过 VM 上部署
helper 的 `service_environment()` 读取，不打印、不写入输出。

输出：打印到 stdout（`HEALTH`/`RESULT`/`TOOL_PARSE`/`OPENAI_FACADE` 等行），并把完整 receipt 写到
VM 私密目录 `validation-<mtp3|off>.json`；回传本机后落在 `logs/qwen-deployment/validation-mtp3.json`
（Git 忽略）。

## `local-tailnet-client.py`

运行位置：Mac（本机，经 Tailnet 访问 VM）。用产品自身的 `RemoteBackend`（而非临时 HTTP 客户端）经
Tailnet 发一次真实生成请求，验证 `discover_remote_model`/`guard_model_identity`、流式生成事件、以及
MTP 草稿/接受计数增量；证明端到端路径（产品代码 + Tailnet + VM 服务）可用，不是只在 VM 回环内验证。

密钥从 `.env.local` 读取（通过 `colab/demo.py` 的 `env_credentials`，读取后立即清空局部变量，不打印）。
算力端主机名不再硬编码：从环境变量 `AGENT_INFER_HOST` 读取，缺省时取第一个命令行参数，都没有则用占位
`<infer-host>`（原脚本跑时的主机名是本次 VM 的临时 Tailnet 节点名，已随 VM 回收失效）。

```bash
AGENT_INFER_HOST=agent-compute.<你的 tailnet>.ts.net \
  /Volumes/enkin/project/Agent/backend/.venv/bin/python benchmarks/qwen-deployment/local-tailnet-client.py
```

输出：打印一行 `TAILNET_PRODUCT_CLIENT_VERIFIED ...`，并把完整结果写到
`logs/qwen-deployment/local-tailnet-client.json`（Git 忽略；该文件含本次运行的 Tailnet 主机名，故不
入库，原件留在本机）。

## 吞吐表原件

`colab/README.md` 表格数据的原始 receipt（`validation-mtp3.json` 等）留在本机
`logs/qwen-deployment/`（Git 忽略，未发布）；该目录下的结果文件记录了当次 VM 的 Tailnet 主机名，
不适合入库。入库的只是本目录的生成脚本。
