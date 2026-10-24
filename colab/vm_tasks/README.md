# VM tasks

每个文件是一段在 Colab VM 上经 `colab exec -f` 执行的独立脚本，由
`compute_ctl.remote_task()` 在文件末尾追加 `main(<JSON 参数>)` 后上传执行。

约束：
- 只用标准库，运行在 VM 的系统 `python3` 上；不得 import 仓库内其他模块。
  需要 VM helper（脱敏、私密环境）的任务按参数里的路径加载它。
- VM 路径、端口、GPU 规格一律来自参数（源头是 `compute_common.py`），
  因此同一份任务能操作新布局和旧 `agent-demo` 布局的 VM。
- 只用一行 `COMPUTE_*` 标记行向本机回报结果；本机只认标记行，不解析其他输出。
