#!/usr/bin/env python3
"""RAG 切片 source_path 前缀迁移（数据根搬迁后的一次性修正）。

背景：B2c 拓扑切换把数据目录（chroma_data/ + storage/）从开发仓 rag/ 拷到
App Support，但切片 metadata 里的 source_path 是入库时写死的**绝对路径**，
搬迁后成为悬空指针。症状（实机 2026-07-18）：管理面板同一文档显示两行——
metadata 行「已索引」+ 磁盘行「未索引」孤儿；更危险的是单篇删除按旧路径动文件，
在双角色机器上会误伤开发仓里的副本。

保守迁移：仅当「旧前缀匹配 && 新路径文件确实存在」才改写该切片的 source_path；
新路径无文件的一律不动（如实保留悬空，宁可面板继续显示异常也不伪造指向）。
幂等：已是新前缀的切片不匹配旧前缀，重跑无害。

用法（rag 服务须停——Chroma 单写者）：
  rag/.venv/bin/python rag/scripts/migrate_source_paths.py \
      --data-root "~/Library/Application Support/dev.enkin.macagent/rag" \
      --old-prefix "/Volumes/enkin/project/Agent/rag/storage" \
      [--dry-run]
（--new-prefix 缺省 = <data-root>/storage）
"""
import argparse
from pathlib import Path

import chromadb


def migrate(data_root: Path, old_prefix: str, new_prefix: str, dry_run: bool) -> None:
    client = chromadb.PersistentClient(path=str(data_root / "chroma_data"))
    total_scanned = total_fixed = total_dangling = 0
    for col_info in client.list_collections():
        col = client.get_collection(col_info.name)
        got = col.get(include=["metadatas"])
        ids, metas = got["ids"], got["metadatas"]
        fix_ids, fix_metas = [], []
        for cid, meta in zip(ids, metas):
            meta = dict(meta or {})
            sp = str(meta.get("source_path") or "")
            if not sp.startswith(old_prefix):
                continue
            total_scanned += 1
            candidate = new_prefix + sp[len(old_prefix):]
            if not Path(candidate).exists():
                total_dangling += 1
                print(f"  ⚠️ [{col_info.name}] 新路径无文件，不动: {candidate}")
                continue
            meta["source_path"] = candidate
            fix_ids.append(cid)
            fix_metas.append(meta)
        if fix_ids:
            print(f"[{col_info.name}] 改写 {len(fix_ids)} 个切片"
                  + ("（dry-run 未写）" if dry_run else ""))
            if not dry_run:
                col.update(ids=fix_ids, metadatas=fix_metas)
            total_fixed += len(fix_ids)
    print(f"\n匹配旧前缀 {total_scanned} 切片：改写 {total_fixed}，悬空保留 {total_dangling}"
          + ("（dry-run，一个字节未写）" if dry_run else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--old-prefix", required=True)
    ap.add_argument("--new-prefix", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    root = Path(args.data_root).expanduser()
    new_prefix = args.new_prefix or str(root / "storage")
    migrate(root, args.old_prefix.rstrip("/"), new_prefix.rstrip("/"), args.dry_run)
