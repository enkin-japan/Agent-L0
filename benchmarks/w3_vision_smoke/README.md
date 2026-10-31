# W-3 视觉端到端验收探针

验收口径：Colab 部署 `google/gemma-4-31B-it-qat-w4a16-ct` 后，一次纯文本 + 一次图文请求走通
（原生协议 `/health` → `/v1/generate` → `/v1/media` + `/v1/generate`，与生产 `RemoteBackend` 同路）。

```bash
INFER_REMOTE_HOST=<host[:port]> INFER_API_KEY=<key> \
  backend/.venv/bin/python benchmarks/w3_vision_smoke/smoke.py
```

- 缺省端口取 config `inference.remote.port`（14161）；`--base-url http://h:p` 可整体覆盖。
- 判定：health 身份 = 上述 repo（revision 钉扎一致、capabilities 含 vision；`--any-model` 跳过）；
  文本回复含 `21`；图文回复含 `W3` 或 `2026`，且含独立字母 `B`。三步全过退出 0。
- 产物：`--out`（缺省 `benchmarks/data/w3-vision-smoke-<YYYYMMDD>/`）下 `result.json` + `w3_smoke.png`。
- 只读本地分词器（tokenizer-only，不下权重）；单次 `max_tokens=128`，思考关。

## 一体化跑法（付费算力铁则，CLAUDE.md「Long tasks」）

```bash
benchmarks/w3_vision_smoke/run_colab.sh
```

起 Colab → 轮询 `deploy.sh status` 到 `phase: ready` → 用 `set -a; . .env.local` 注入密钥跑探针
→ **无论结果都 `deploy.sh stop`**。主会话用 `Bash run_in_background` 起它，全程不依赖被唤醒。
环境变量 `MODEL` / `MINUTES`（默认 40）/ `OUT`；时间线在 `$OUT/run_colab.log`，终态在 `$OUT/DONE`。
2026-10-31 第一次验收就是因为探针等主会话唤醒而没发出去，A100 就绪 46 分钟后租约到期，才改成这个形态。
