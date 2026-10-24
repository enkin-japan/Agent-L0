# Colab 计算端

从本机 Colab CLI 创建独立的 A100 会话，把当前工作树的计算端源码白名单上传过去，
由 `devctl.sh start infer|rag-compute` 在 VM 上启动推理与 RAG 计算面；数据面留在 Mac。
RAG 计算使用 VM 的 CPU，生成模型独占 GPU。不会上传 `.env*`、`.keys/`、会话、知识库或整个仓库。
Colab 上只使用合成提示与测试资料。

唯一入口是 `./colab/deploy.sh`。旧一代的 notebook / 日用 / bench 脚本已迁到
[`colab/legacy/`](legacy/README.md)，不要与本流程混用（尤其不要运行旧保活链）。

## 前置

- 本机已装 `uv`、`colab`（google-colab-cli）、`tailscale`，且 Tailscale 在线、tailnet 已启用 MagicDNS。
- Tailnet auth key 手工填入 `.keys/tailnet-colab.authkey`（父目录 700，文件 600）。一键重建需要有效的
  **Reusable（可复用）** key；单次 key 首次入网即消耗。VM 使用 `tailscaled --state=mem:` 强制注册
  ephemeral 临时节点，不依赖 key 是否勾选 Ephemeral，也不复用旧 VM 的节点身份。key 仍应最小权限。
- 本机 `.env.local` 的 `INFER_API_KEY`、`RAG_COMPUTE_API_KEY` 已配置且不少于 16 字符。脚本只传这两项及
  可选 `HF_TOKEN`，不显示其值；auth key 经私密文件交给 Tailscale，不放命令参数。CLI 凭据与会话文件由
  CLI 自己管理，脚本从不读取或改写。

`deploy.sh` 用 `uv` 在 `.colab-compute/launcher-venv` 建固定的轻量环境（PyYAML 6.0.3）。该环境在启动终端
退出后仍保留，供后台进程做 DNS 与健康检查；租约运行期间不要删除 `.colab-compute/`。

## 命令

```bash
./colab/deploy.sh prepare                  # 打包检查；不读密钥、不创建 VM
./colab/deploy.sh models                   # config.yaml 中钉了 revision 的 CUDA 模型
./colab/deploy.sh start --minutes 120      # 默认后台部署；命令立即返回，不代表已 READY
./colab/deploy.sh start --retain           # READY 后一直保留，直到手动 stop
./colab/deploy.sh start --foreground       # 前台模式；Ctrl+C 请求回收，请等待最终确认
./colab/deploy.sh switch --model <repo>    # 在现有 VM 上切换模型或推理选项（见下）
./colab/deploy.sh watch --retain           # 监督进程退出后，为已有 READY VM 重新挂监督；不新建 VM
./colab/deploy.sh logs -f                  # 跟随部署进度；Ctrl+C 只退出查看器
./colab/deploy.sh logs --remote --lines 80 # VM 上脱敏后的部署日志末尾（快照，最多 64 KiB）
./colab/deploy.sh status                   # 租约、监督进程、服务端 VM、本机可见遗留节点、CLI 版本
./colab/deploy.sh stop                     # 注销临时节点、释放本工具的 VM，并核对服务端
./colab/deploy.sh <命令> --help            # 每个命令只接受与它相关的选项
```

`start` / `switch` 的部署选项：`--model <完整 repo>`、`--quantization none|fp8`、
`--speculative off|mtp`（默认 off）、`--draft-tokens 1..5`（只配 mtp，默认 3）、
`--attention-backend auto|triton|flashinfer`、`--kv-cache-dtype auto|bfloat16|fp8`、
`--max-model-len`（默认 32768）、`--gpu-memory`（默认 0.72）、`--stall-timeout`（默认 600 秒）、
`--bootstrap-timeout`（默认 3600 秒）、`--poll-interval`（默认 10 秒）。`start` 另有 `--minutes`（1–240）、
`--hostname`（默认 `agent-compute`）、`--auth-key`、`--retain`、`--foreground`、`--health-interval`（默认 60 秒）。
默认值和取值范围各只在 `compute_ctl.py` 的 `DEFAULTS` / `LIMITS` 里定义一次。

后台进程独立于启动终端，关闭终端不影响部署；停止必须用 `stop`。后台 worker 的全部参数都从租约
记录读取（命令行只传租约身份），不会出现前台校验与后台执行用了两套参数的情况。后台启动成功只说明
已交给工作进程，是否部署完成以日志的 `READY` 与 `status` 为准。

## 模型与 MTP

模型只能选 `models` 列表里的 CUDA 条目，不能把 MLX 权重交给 CUDA。BF16 Gemma 原件超过 A100 40GB，须显式
`--quantization fp8`；列表只表示已注册，其他组合的显存、质量和速度仍需实测。Qwen3.8-27B 注册为
`RedHatAI/Qwen3.8-27B-INT4`（revision `91bd022d5b49442a868bc35008f6c21e1860edfa`）：CUDA compressed-tensors
INT4 权重，自带原生 MTP 头，无需另下 MLX drafter；`--quantization none` 表示不再在线二次量化。

```bash
# 新建 VM（仅在没有现存租约时）
./colab/deploy.sh start --retain --model RedHatAI/Qwen3.8-27B-INT4 \
  --speculative mtp --draft-tokens 3 --gpu-memory 0.85 --max-model-len 32768

# 复用已有 READY VM：后台切换推理，保留 VM、Tailnet 名称和 RAG 服务
./colab/deploy.sh switch --model RedHatAI/Qwen3.8-27B-INT4 \
  --speculative mtp --draft-tokens 3 --gpu-memory 0.85 --max-model-len 32768

# 同一模型关闭 MTP；资源参数每次显式传入
./colab/deploy.sh switch --model RedHatAI/Qwen3.8-27B-INT4 \
  --speculative off --attention-backend triton --kv-cache-dtype bfloat16 \
  --gpu-memory 0.85 --max-model-len 32768
```

Qwen+MTP 的 auto 解析为 **TRITON_ATTN + bfloat16**（此前 A100 实测的组合）；其他模型保持原有 auto 行为。
MTP 只允许注册表明确声明 `supports_mtp` 的模型。服务并发与 vLLM `max_num_seqs` 均设为 1，避免只限 HTTP
并发而底层仍按大并发预分配缓存。显式的其他 attention/KV 组合交给当前 vLLM 验证，不承诺 A100 都支持。
MTP 收益随输出内容、提示长度和草稿接受率变化，`--draft-tokens` 越大不保证越快。

## 切换（switch）

`switch` 核验当前租约与服务端 endpoint 完全一致，不新建或释放 VM，不重新加入 Tailnet。它把源码与新版
helper 上传到隔离的 staging，核对推理鉴权及空闲，再 `devctl.sh stop infer`，确认 GPU 无计算进程后更新
推理代码与配置、下载/加载模型，最后核验真实 `/health` 的 repo、revision、MTP/attention/KV 参数及 RAG 身份。
活动依据鉴权 `/metrics.active_gpu_work`（含 OpenAI 门面请求）；正在生成或活动未知时拒绝切换，缺该字段的
旧服务须在维护窗口确认调用方已停、在原 VM 上 `devctl.sh stop infer` 后重试。远端锁防止遗留 worker 与重试
同时加载模型；`stop` 标记及本地状态 CAS 防止迟到的 READY 复活租约。切换期间计算端不可用。

- 失败保存 `switch-failed` 与日志，保留 VM 供排查，可重试；不会自动回滚模型或申请新 VM。本地 CLI 超时
  不代表远端 worker 已结束，先看 `logs --remote`。
- `switch --foreground` 的 Ctrl+C 只中断本地等待，VM 保留；回收仍用 `stop`。
- 切换保持现有租期及 `retain`，不延长租期。缺少 `switch_aware` 监督标记的旧版**限时**租约会被拒绝
  （其监督进程可能在停服期间回收 VM），须先正常 `stop` 再 `start`；`switch --retain` 不能绕过。
  `retain: true` 的租约可直接切换。
- `/health.inference_options` 只证明启动配置；MTP 是否真在执行，看鉴权 `/metrics.speculative_decode` 中
  `draft_tokens`、`accepted_tokens`、`num_drafts` 的生成前后增量，`unknown` 或零增量都不算证明。
- 执行端需使用含该模型注册项的 `config.yaml`；切换完成后重启执行层，让它重新发现模型身份并采用对应
  方言（旧进程会拒绝向身份不符的模型发请求）。部署脚本不改本机 App 的配置、签名或安装包。

## 保留与监督（retain / watch）

`--retain` 是显式手动保留：启动失败或超时仍回收未就绪 VM；READY 后不再按租期回收，健康失败只记录告警
并继续监督，只有手动 `stop` 才注销节点并释放 VM。`status` 中 `retain: true` 表示原 `deadline` 仅用作启动预算。

`watch --retain` 只接管当前租约记录中身份核对一致的 READY VM；已有监督进程持锁时拒绝重复接管。它用于
监督进程意外退出后的恢复，不读凭据、不建 VM、不加载模型。`watch` 返回 0 只表示监督已启动，以 `status`
中 `supervision.state: active` 和日志「后台监督已确认运行」为准；`interrupted` 表示监督已退出但 VM 仍保留。
Colab 自身仍可能回收实例，Mac 休眠/断网也会中断监督；手动保留不保证永久在线。

默认限时模式 READY 后每 60 秒通过 Colab kernel 检查推理及 RAG 健康，连续三次失败即记录并尝试回收，不会悄悄
申请新 VM。这是真实服务监督，没有 GPU 空转、模拟点击或已废止的 keepalive API。CLI 0.7.5 没有独立保活守护
进程；后端按 kernel 活动维护会话，但 Colab 仍可因空闲、资源策略或最长生命周期回收 VM，参见
[CLI 会话说明](https://github.com/googlecolab/google-colab-cli/blob/main/docs/01_session_management.md) 与
[Colab FAQ](https://research.google.com/colaboratory/faq.html)。

## 回收与资源核对

- 启动前读取 Colab 服务端列表并检查本地租约：即使本地标为 stopped，只要端点仍在就阻止重建。发现本工具
  创建的其他 VM（`agent-compute-*`，以及旧布局的 `agent-demo-*`）、身份未知的 `?` VM、可见的遗留或同名节点
  （包括离线节点）都会阻止新建，先运行 `status` 排查。
- 只释放当前租约的端点；创建响应丢失时，按创建前已保存的精确随机会话名恢复端点。不会按名称前缀批量删除
  设备或会话。状态更新使用独立文件锁，关停后到达的旧进度不能把租约重新标为运行中。
- 完整关停另有互斥锁：另一进程正在关停时等待其结果，成功则退出，失败或中断则接手。回收期间屏蔽重复中断
  信号，最多三轮尝试，只有服务端端点消失才打印释放确认；否则记录 `release-unconfirmed`，阻止重建，
  不能视为已回收，需重试 `stop`。启动的收尾即使本地状态文件丢失或损坏，也按本进程所知的租约回收已分配的 VM。
- 预检查期间 `stop` 会阻止后续申请 VM；分配期间 Ctrl+C 会等当前申请返回（上限 120 秒）并记录端点后立即回收。
- 关停先尝试 `tailscale logout`，无论成败都释放已知端点并核对服务端。`current.json` 分开记录 VM 状态与
  `tailnet_cleanup`：`ephemeral-logout-confirmed` 表示临时节点注销成功；`logout-unconfirmed` 表示未确认，
  不能宣称已删除；普通节点即使注销成功也记 `logout-confirmed-removal-unverified`。失联的临时节点通常由
  Tailscale 在最后活动后约 30–60 分钟自动删除，期间可能阻塞同名重建。
- CLI 0.7.5 的 `stop` 在 exec 清掉本地记录后会静默成功，因此端点仍在时脚本会用已安装 CLI 自己的客户端按
  端点释放。这依赖 CLI 私有接口，只对 `compute_ctl.py` 中 `VERIFIED_COLAB_CLI` 列出的版本放行；其他版本
  直接报错并记 `release-unconfirmed`，请在 Colab 控制台手动释放后再 `stop` 核对。`status` 会显示当前 CLI 版本。
- 本机节点视图受 ACL 限制，不能证明管理后台没有不可见的遗留节点；遗失租约或旧非临时节点需在管理后台核对
  身份后删除。脚本不需要 Tailscale 管理 API 密钥。

## 网络与 DNS

计算端默认使用固定名称 `agent-compute.<tailnet>.ts.net`，可用 `--hostname` 改名。**MagicDNS 固定的是名称，
不是 IP**；临时节点重建后 IP 可能变化。Mac/iPhone 应在同一 tailnet 并接受 Tailscale DNS 设置。VM 的
`--accept-dns=false` 只表示 VM 不接管自身 DNS。READY 前会核对新节点获配的名称精确匹配，并通过 Mac 系统解析器
确认名称指向本次 VM；名称冲突产生 `-1` 后缀会失败并释放 VM。参见
[临时节点](https://tailscale.com/docs/features/ephemeral-nodes)、[MagicDNS](https://tailscale.com/docs/features/magicdns)
与[机器名称冲突](https://tailscale.com/docs/concepts/machine-names)。

VM 上的 Tailscale 安装官方静态包，版本与 sha256 钉在 `compute_vm.py`（`TAILSCALE_VERSION` /
`TAILSCALE_SHA256`），校验不符即拒绝安装；不执行任何远程安装脚本。升级时两个常量一起改。

终端显示 `READY` 后，把 host 和上述两把服务 key 填入 MacAgent 设置页。

## 日志与诊断

日志在 `.colab-compute/deploy.log`；后台进程的 stdout/stderr（只剩 traceback 一类）在 `worker.log`；新一轮开始时
上一轮保留为 `.previous`。日志覆盖源码打包、每个文件上传、Python 与依赖安装、入网、模型配置、下载（已下载/总
GiB、百分比、速度）、模型加载/编译与鉴权检查；加载阶段持续转发脱敏后的推理/RAG 日志。每条进度带总用时与
无进展时长；无进展达到 `--stall-timeout` 或超过整体时限即诊断并回收（`switch` 超时保留 VM）。

启动失败时，释放 VM 前会把脱敏服务诊断保存到 `.colab-compute/failure-<会话名>.log`（权限 600，每份服务日志
保留末尾 64 KiB）；VM 不可达时明确报告诊断收集失败，仍继续释放。脱敏在 VM 上完成，传回本机前就已去掉密钥。
进度查询遇到暂时超时或 CLI 错误会有界重试三次，并受启动与租期时限约束。

## 资源与性能记录

默认请求 A100，装模型前验证为单张 40GB 卡且无已有计算进程；分配到 80GB 会释放并报错，不自动扩大开销。
VM 的 `config.yaml` 把推理显存上限设为 `--gpu-memory`、并发 1、上下文 `--max-model-len`；Mac 的配置不受修改。
`enforce_eager=False` 保留 torch.compile 与 CUDA Graph，首次启动需要编译和捕获。完整 CUDA 环境由 uv 建立
Python 3.12 隔离环境（避开 Colab 系统 Python 缺 ensurepip）；直接依赖见 `requirements-compute.txt`，Linux
解析版本锁在 `requirements-compute.lock`，实际安装版本写入 VM 私密部署日志。

A100 上 NVFP4 实际走 Marlin W4A16，不是原生 FP4/FP8 运算。2026-10-23 同一 A100 40GB、26B NVFP4、单请求、
256 token 输出、各预热一次后测三次：51 token 提示的 decode 中位数从 eager 的 11.81 升至 163.46 token/s；
13,704 token 提示从 11.71 升至 122.78 token/s（VM 回环 SSE，排除首 token，正式样本命中前缀缓存；Graph 配置下
总显存约 38,401 MiB，含 RAG）。此前 App 中的 12.1 token/s 来自强制 eager 的配置，不代表该卡的正常水平。

2026-10-24 通过正式 `switch` 在同一 A100 40GB 上完成 Qwen3.8 的 MTP 开 → 关 → 开验证，保持 RAG 与 VM。
配置为上下文 32768、显存 0.85、单请求、Triton/BF16 KV，启用前缀缓存与 CUDA Graph；每类合成提示预热一次后测
三次，输出截至 256 token，以下为去除首 token 后的 decode 中位数（token/s）：

| 提示 | MTP 关闭 | MTP 3 token |
| --- | ---: | ---: |
| 短文（79 token 提示） | 64.9 | 84.3 |
| 代码（78 token 提示） | 64.9 | 131.0 |
| 长提示（13,722 token） | 58.8 | 61.2 |

这只是少量、固定顺序、重复提示的吞吐对照：短文和代码输出不逐字一致，不构成质量或无损性评测；decode 提升也
不代表端到端延迟同比改善（长提示预热后首 token 中位数约 0.26 / 0.50 秒）。真实 MTP 计数增量、工具 XML 解析、
OpenAI 门面与生成期间拒绝切换均有验收；本机 `RemoteBackend` 经 Tailnet 的请求也确认草稿 129 / 接受 107 token。
注意：该轮工具解析验收是关闭思考的单段生成；开启思考的两段式生成在 MTP 下会丢失工具调用开头的问题，已在
`backend/framework/backends/cuda/backend.py` 修复（`_trim_after_stop`），随下一次 `switch` / `start`
上传到 VM 后生效。结果原件在本机 `logs/qwen-deployment/`
（Git 忽略，未发布）。合成基准不等同于所有 App 请求或多模态负载的性能保证。

## 代码结构

| 文件 | 运行位置 | 职责 |
| --- | --- | --- |
| `deploy.sh` | Mac | 唯一入口：准备 launcher venv 后转交 `compute_ctl.py` |
| `compute_ctl.py` | Mac | 租约状态机、资源核对、上传、进度跟踪、监督、切换、回收 |
| `compute_common.py` | Mac 与 VM | 两侧共用的事实：私密 `.env` 语法、密钥判定、脱敏、VM 布局、端口、GPU 规格 |
| `vm_tasks/*.py` | VM（`colab exec`） | 每个远端动作一个独立脚本，参数由本机注入；见 [vm_tasks/README.md](vm_tasks/README.md) |
| `compute_vm.py` | VM | 首次部署（环境、Tailnet、模型配置、起服）与切换事务 |
| `compute_fetch.py` | VM | 观察产品模型解析器的下载进度并校验 revision |
| `test_*.py` | Mac | CPU-only 测试：假 colab CLI、临时 git 仓与状态目录；`./devctl.sh check --only colab` |

本机状态目录 `.colab-compute/`（Git 忽略）：`current.json` 租约记录、各类锁、`deploy.log` / `worker.log`、
失败诊断与 `launcher-venv`。

### 旧布局兼容

2026-10-24 之前创建的租约没有 `vm_layout` 字段，VM 上的路径是 `/content/agent-demo*`、会话名是
`agent-demo-*`，VM 上的 helper 是旧版 `demo_vm.py`（经 PATH 上的 `tailscale` 入网）。新代码按
`compute_common.LEGACY_LAYOUT` 继续管理这类租约（stop / switch / logs / watch）；新建的租约一律使用新布局。

旧租约的本机记录原在 `logs/colab-demo/`，需一次性迁到 `.colab-compute/`（新代码只读后者）：

1. 结束旧监督进程：对 `status` 里旧的 `_watch` / `_worker` pid 发 SIGTERM。它会按原设计把监督标为
   `interrupted` 并保留 retained VM；**不要**对非保留的限时租约这样做（它会开始回收）。
2. 把 `logs/colab-demo/current.json` 复制为 `.colab-compute/current.json`（权限 600）；`deploy.log` 可一并复制
   以保留历史。锁文件不复制，新代码会重建。`logs/colab-demo/` 原样留作存档。
3. `./colab/deploy.sh watch --retain` 用新代码重新挂上监督，再用 `status` 确认 `supervision.state: active`。

`status` 中不再有缺少 `vm_layout` 的租约后，可以删掉 `LEGACY_*` 与相关分支。
