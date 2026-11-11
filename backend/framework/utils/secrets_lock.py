"""数据根 api_tools/.secrets.env 的进程内写锁（共享）。

api_tool_factory.write_secret 是「读全文件 → 去旧行 → 写回」的读改写，本身不加锁；
两个模块（搜索 API 提供方、邮件发送设置）各自持自己的锁去写同一个文件会互相覆盖。
凡是调 write_secret 的设置模块都在这把锁里调（可重入：持有方可再进自己的存储段）。
"""
import threading

SECRETS_FILE_LOCK = threading.RLock()
