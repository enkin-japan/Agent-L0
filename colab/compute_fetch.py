#!/usr/bin/env python3
"""Observe the existing model resolver's download; do not create another cache.

Runs on the VM under the CUDA venv. compute_vm.py passes the VM layout through
COMPUTE_VM_PRIVATE / COMPUTE_VM_APP so the same file serves current and legacy VMs.
"""
import json
import os
from pathlib import Path
import sys
import threading
import time

PRIVATE = Path(os.environ.get('COMPUTE_VM_PRIVATE', '/content/agent-compute-private'))
APP = Path(os.environ.get('COMPUTE_VM_APP', '/content/agent-compute'))
REPORT_INTERVAL_S = 2


def downloaded_bytes(root, files):
    """Count completed payloads plus HTTP partials, excluding cache metadata."""
    total = 0
    for name, size in files.items():
        try:
            total += min(size, (root / name).stat().st_size)
        except FileNotFoundError:
            pass
    for path in (root / '.cache/huggingface/download').rglob('*.incomplete'):
        try:
            total += path.stat().st_size
        except FileNotFoundError:
            pass  # A concurrent atomic rename is observed next time.
    return min(sum(files.values()), total)


def write_report(value):
    temporary = PRIVATE / 'download.tmp'
    temporary.write_text(json.dumps(value))
    temporary.replace(PRIVATE / 'download.json')


def report_until(finished, repo, root, files):
    total = sum(files.values())
    previous, previous_time = 0, time.monotonic()
    while True:
        count = downloaded_bytes(root, files)
        now = time.monotonic()
        write_report({'repo': repo, 'bytes_downloaded': count, 'bytes_total': total,
                      'percent': round(100 * count / max(1, total), 1),
                      'bytes_per_second': round(max(0, count - previous)
                                                / max(.01, now - previous_time))})
        if finished.is_set():
            return
        previous, previous_time = count, now
        finished.wait(REPORT_INTERVAL_S)


def main():
    os.umask(0o077)
    sys.path.insert(0, str(APP / 'backend'))
    from huggingface_hub import HfApi
    from framework.utils.model_selection import resolve_selected_model
    from framework.utils.path_resolver import resolve_model_path, _models_root
    repo, revision, _ = resolve_selected_model()
    root = _models_root() / Path(repo).name
    print('查询模型文件清单和总字节数：' + repo, flush=True)
    info = HfApi().model_info(repo, revision=revision, files_metadata=True, timeout=30)
    files = {item.rfilename: item.size for item in info.siblings}
    if not files or any(not isinstance(size, int) or size < 0 for size in files.values()):
        raise RuntimeError('拿不到模型文件大小，无法可靠报告下载进度')
    finished = threading.Event()
    observer = threading.Thread(target=report_until, args=(finished, repo, root, files),
                                daemon=True)
    observer.start()
    try:
        resolve_model_path(repo, revision=revision)
        marker = root / '.download_complete'
        if not marker.exists() or json.loads(marker.read_text()).get('revision') != revision:
            raise RuntimeError('模型解析器返回时没有已校验的下载完成标记')
        print('模型下载和 revision 校验完成', flush=True)
    finally:
        finished.set()
        observer.join(timeout=5)


if __name__ == '__main__':
    main()
