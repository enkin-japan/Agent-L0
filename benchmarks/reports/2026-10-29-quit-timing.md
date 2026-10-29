# ⌘Q 退出耗时实测（2026-10-29，本机 M4 Max，/Applications/MacAgent.app 装机版）

口径：`benchmarks/quit_measure.sh`——`open -a` 启动 → 按模式等就绪 → `osascript -e 'tell application "MacAgent" to quit'`
起计时 → `pgrep -x MacAgent` 每 50 ms 轮询到进程消失。计算端域名（tailnet）当时不可达，引擎未装配；
数字覆盖「本机三服务 + 引擎未装配」的退出路径，不含引擎释放。

## 修前（build 81 初装，退出路径与 80 相同；转录自会话工具输出，未留原始文件）
当时的脚本是本文件所附脚本的前身：标签打的是 `QUIT_ELAPSED_BEFORE`（现脚本打 `QUIT_ELAPSED`），
「listeners after quit」后面直接列监听行、无残留时为空（现脚本打计数）。以下按当时输出原样转录：
```
ready=0 after 70 s          # 就绪判定用了旧日志行，实际等了 70 s 才退，服务早已全起
QUIT_ELAPSED_BEFORE=7.00s
listeners after quit:       # 空 = 无残留
```
同次 backend.log（已轮转为 .prev）：`Shutting down` → `Waiting for connections to close.` →
`ERROR: Cancel 1 running task(s), timeout graceful shutdown exceeded` → CancelledError 落在 listen_for_disconnect。

## 修后，全就绪（三端口监听 + 8 s；转录自会话工具输出，未留原始文件）
```
QUIT_ELAPSED=1.20s
listeners after quit: 0
34:INFO:     Shutting down
36:INFO:     Application shutdown complete.
37:INFO:     Finished server process [95548]
[2026-10-29T08:06:52Z] down rc=0 state=running
[edge] 停止 backend（已验证本实例父子进程身份）
[edge] backend 本实例进程已停止（其他端口占用者不受影响）
[edge] 停止 SearXNG（已验证本实例父子进程身份）
[edge] SearXNG 本实例进程已停止（其他端口占用者不受影响）
[edge] 停止 RAG（已验证本实例父子进程身份）
[edge] RAG 本实例进程已停止（其他端口占用者不受影响）
```

## 修后，启动中退出（backend 一监听即退；原始文件 run-early2.txt 原样）
```
mode=early ready=1 after 2 s
QUIT_ELAPSED=1.09s
listeners after quit: 0
12:INFO:     Shutting down
14:INFO:     Application shutdown complete.
15:INFO:     Finished server process [96240]
--- sidecar-down.log tail
[edge] 停止 backend（已验证本实例父子进程身份）
[edge] backend 本实例进程已停止（其他端口占用者不受影响）
[edge] 停止 SearXNG（已验证本实例父子进程身份）
[edge] SearXNG 本实例进程已停止（其他端口占用者不受影响）
[edge] 停止 RAG（已验证本实例父子进程身份）
[edge] RAG 本实例进程已停止（其他端口占用者不受影响）
[2026-10-29T08:07:32Z] down rc=0 state=starting
[edge] 停止 backend（已验证本实例父子进程身份）
[edge] backend 本实例进程已停止（其他端口占用者不受影响）
[edge] SearXNG 无本实例运行记录
[edge] 停止 RAG（已验证本实例父子进程身份）
[edge] RAG 本实例进程已停止（其他端口占用者不受影响）
--- pidfiles:
```

## 修后，searx 已拉起未就绪时退出（原始文件 run-searx.txt 原样）
```
mode=searx-spawning ready=1 after 3 s
QUIT_ELAPSED=1.17s
listeners after quit: 0
24:INFO:     Shutting down
26:INFO:     Application shutdown complete.
27:INFO:     Finished server process [96596]
--- sidecar-down.log tail
[edge] 停止 backend（已验证本实例父子进程身份）
[edge] backend 本实例进程已停止（其他端口占用者不受影响）
[edge] SearXNG 无本实例运行记录
[edge] 停止 RAG（已验证本实例父子进程身份）
[edge] RAG 本实例进程已停止（其他端口占用者不受影响）
[2026-10-29T08:08:22Z] down rc=0 state=starting
[edge] 停止 backend（已验证本实例父子进程身份）
[edge] backend 本实例进程已停止（其他端口占用者不受影响）
[edge] 停止 SearXNG（已验证本实例父子进程身份）
[edge] SearXNG 本实例进程已停止（其他端口占用者不受影响）
[edge] 停止 RAG（已验证本实例父子进程身份）
[edge] RAG 本实例进程已停止（其他端口占用者不受影响）
--- pidfiles:
```

## 修后复测留档（见文末追加段，脚本直接写入）

```
mode=full ready=1 after 4 s
QUIT_ELAPSED=1.26s
listeners after quit: 0
34:INFO:     Shutting down
36:INFO:     Application shutdown complete.
37:INFO:     Finished server process [20943]
--- sidecar-down.log tail
[edge] backend 本实例进程已停止（其他端口占用者不受影响）
[edge] 停止 SearXNG（已验证本实例父子进程身份）
[edge] SearXNG 本实例进程已停止（其他端口占用者不受影响）
[edge] 停止 RAG（已验证本实例父子进程身份）
[edge] RAG 本实例进程已停止（其他端口占用者不受影响）
[2026-10-29T09:39:54Z] down rc=0 state=running
[edge] 停止 backend（已验证本实例父子进程身份）
[edge] backend 本实例进程已停止（其他端口占用者不受影响）
[edge] 停止 SearXNG（已验证本实例父子进程身份）
[edge] SearXNG 本实例进程已停止（其他端口占用者不受影响）
[edge] 停止 RAG（已验证本实例父子进程身份）
[edge] RAG 本实例进程已停止（其他端口占用者不受影响）
--- pidfiles: 
```
