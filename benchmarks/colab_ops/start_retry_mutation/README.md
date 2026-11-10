# `start` 阻塞重试的变异证明（2026-11-10）

对象：`colab/compute_ctl.py` 的 `start_until_allocated` 一族守卫（只重试已证明为空的失败、stop 标记、
中断回收、首次联络时限、停机后不再部署）。每条变异是一处精确的字符串替换（`old` → `new`），在源码里
必须恰好出现一次；注入后清 `colab/__pycache__`，跑

```
backend/.venv/bin/python -m pytest colab/test_compute_ctl.py colab/test_compute_deploy.py -q -p no:cacheprovider \
  -k 'StartRetryTests or retries_failed or retry_loop or release_unconfirmed or not_retried or being_refused or first_contact'
```

套 240 s 硬超时（死循环也算变红），认 `FAILED` / `ERROR` / `SUBFAILED` 三种行，之后逐字节还原并核对哈希。

| 轮次 | 清单 | 当时的源码 | 结果 |
|---|---|---|---|
| 1 | `round1_mutants.json`（23 条） | 加 stop 标记闭环与瞬时拒绝判定之后、放宽首次联络之前 | 23 / 23 变红（`round1_results.json`；M14 以硬超时判红） |
| 1 补 | N1 不保留属主刚得知的事实；N2 把 `allocation_attempted` 也算进保留事实 | 同上，加 `RELEASE_FACTS` 之后 | 2 / 2 变红 |
| 2 | `round2_mutants.json`（7 条） | 首次联络 3 分钟、`await_boot` 之后 | 7 / 7 变红（A3 以硬超时判红） |
| 2 补 | B1 上传前不查 stop；B2 启动部署前不查 stop；B3 查 stop 时不看标记；B4 中断回收后不通知后台进程退出 | 最终版 | 4 / 4 变红 |

轮次 1 的 `old` 文本有两处在轮次 2 被改写（`await_allocation` → `await_boot`），不能直接在最终版上重放；
对应的守卫由轮次 2 的 A1–A3 覆盖。逐条变红的测试名见提交信息。
