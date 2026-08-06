"""CUDA 推理后端（vLLM）：InferenceBackend 协议的 NVIDIA 实现（HETERO_COMPUTE A 线）。

与 mlx/ 同格局：上层只见协议不见实现名。vllm/torch 均为惰性导入——本包在
无 CUDA 环境（开发 Mac）可安全 import，金测试据此测协议形状而不装 vLLM。
"""
