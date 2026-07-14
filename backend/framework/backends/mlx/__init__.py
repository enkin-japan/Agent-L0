"""MLX 后端包（Apple Silicon 进程内推理）。

刻意保持空壳：backend.py 顶层 import mlx，经包 __init__ 提前加载会把重型依赖
带进只想拿 ASR provider 的路径。取用一律走 framework.backends.get_backend_class。
"""
