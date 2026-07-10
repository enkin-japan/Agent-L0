"""前台模型下载器：devctl.sh 在启动 backend 前调用。

目的只有一个：把 HF 下载进度条直接呈现在用户终端。此前下载发生在 backend 守护进程的
lifespan 里，进度全部落日志，devctl 只能打点等待——初次部署要静默下数 GB，观感如死机。
本脚本在前台跑同一个 resolve_model_path（huggingface_hub 自带 tqdm 进度条，TTY 直显、
非 TTY 自动降级为间歇行），成功后写下 .download_complete 标记；随后 backend 启动时
命中标记秒过。app.py 内的下载路径原样保留，作为绕过 devctl 直启时的自愈兜底。

退出码：0 = 模型就绪；非 0 = 下载失败（devctl 据此中止 backend 启动）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from framework.utils.model_selection import resolve_selected_model
from framework.utils.path_resolver import resolve_model_path


def main() -> int:
    repo, revision, source = resolve_selected_model()
    origin = "启动选择 (.model-selection)" if source == "selection" else "config.yaml 默认"
    print(f"📦 模型: {repo}" + (f" @ {revision[:8]}" if revision else "") + f"（{origin}）")
    try:
        path = resolve_model_path(repo, revision=revision)
    except Exception as e:
        print(f"❌ 模型准备失败: {e}", file=sys.stderr)
        return 1
    print(f"✅ 模型就绪: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
