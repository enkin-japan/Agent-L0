#!/usr/bin/env python3
"""RAG 向量库全量重建（换档 / 8B 升级用）——HETERO_COMPUTE B2a / §3.3。

背景（换档纪律）：向量库一旦入库，档位即固定。换嵌入档位（尺寸/维度变）后，旧库维度
不再匹配，rag 服务启动/入库会被换档护栏拦下（rag_server 的 _guard_or_stamp_store）。
本脚本按当前档位从 storage/raw_files 原文备份**全量重灌**（走 rag 服务既有 /insert
流水线，切块参数不变），把库重建成新档位维度。

原文备份是重建的承重网（CHARTER §0）：raw_files/{分类}/*.txt 按分类目录组织，
unclassified_raw/*.txt 为未分类池。分类的**路由描述**只存在 meta 库里（会随重建被清），
故重建前须先 dump-categories 备份描述。

标准换档流程（devctl 管理服务生命周期；过渡期手动流程——正式口径为执行端 app/数据面驱动，见 rag/README §档位与换档）：
  1) ./devctl.sh rag-model                          # 选新档位（写 .rag-tier-selection）
  2) python rag/scripts/reembed.py dump-categories  # 旧服务在跑时备份分类描述
  3) ./devctl.sh stop rag
  4) python rag/scripts/reembed.py wipe             # 删 chroma_data（换新维度的空库）
     rm -f rag/fts_index.sqlite3*                   #   FTS 索引连带清（fail-open，重灌自动重建）
  5) ./devctl.sh start rag                          # 新档位加载 + 空库盖新戳
  6) python rag/scripts/reembed.py rebuild          # 重建分类 + 全量重灌原文
  7) （§3.3 生产：新旧库并行查询对比 → 切流 → 旧库归档；本脚本只管重建）

鉴权：读 env RAG_API_KEY（与服务端同值），自动携带 Bearer。开发机验证可用 --dry-run。
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RAG_DIR = REPO_ROOT / "rag"
# 与 rag_server.py 同一套环境变量覆盖（B2c）：RAG_CONFIG 指配置、RAG_DATA_DIR 指数据根
# （bundle/测试布局；缺省 = 仓库现状）
CONFIG_PATH = Path(os.environ.get("RAG_CONFIG", "").strip() or REPO_ROOT / "config.yaml")
DATA_ROOT = Path(os.environ.get("RAG_DATA_DIR", "").strip() or RAG_DIR)
STORAGE = DATA_ROOT / "storage"
RAW_FILES = STORAGE / "raw_files"
UNCLASSIFIED = STORAGE / "unclassified_raw"
CHROMA_DB = DATA_ROOT / "chroma_data"
CATEGORIES_BACKUP = STORAGE / "categories_backup.json"

_cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
_rag = _cfg["rag"]
BASE_URL = f"http://{_rag['host']}:{_rag['port']}"


def _headers() -> dict:
    key = os.environ.get("RAG_API_KEY", "")
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _server_alive() -> bool:
    try:
        requests.get(f"{BASE_URL}/health", timeout=3)
        return True
    except Exception:
        return False


def _require_server() -> dict:
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=5)
        r.raise_for_status()
    except Exception as e:
        sys.exit(f"❌ rag 服务不可达（{BASE_URL}）：{e}\n   请先 ./devctl.sh start rag。")
    return r.json()


def _category_dirs() -> list:
    if not RAW_FILES.exists():
        return []
    return sorted([p for p in RAW_FILES.iterdir() if p.is_dir()])


def _title_from_filename(path: Path) -> str:
    # 备份命名为 {safe_title}_{uuid8}.txt（见 rag_server.auto_classify_and_insert）
    return path.name.rsplit("_", 1)[0] if "_" in path.name else path.stem


def cmd_dump_categories(args) -> None:
    info = _require_server()
    print(f"服务在线：档位={info.get('tier')} dim={info.get('embed_dim')}")
    r = requests.get(f"{BASE_URL}/api/v1/category", headers=_headers(), timeout=15)
    r.raise_for_status()
    cats = r.json()
    CATEGORIES_BACKUP.write_text(json.dumps(cats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✅ 备份 {len(cats)} 个分类路由描述 → {CATEGORIES_BACKUP}")


def cmd_wipe(args) -> None:
    if _server_alive():
        sys.exit("❌ 请先停止 rag 服务（./devctl.sh stop rag）再 wipe——运行中删库会崩。")
    if not CHROMA_DB.exists():
        print("chroma_data 不存在，无需 wipe。")
        return
    if not args.yes:
        ans = input(f"将删除 {CHROMA_DB}（向量库全清，原文 raw_files 保留）。确认? [y/N] ").strip().lower()
        if ans != "y":
            sys.exit("已取消。")
    shutil.rmtree(CHROMA_DB)
    print(f"🗑️ 已删除 {CHROMA_DB}（下次 start rag 将建空库并盖新档位戳）。")


def cmd_rebuild(args) -> None:
    info = _require_server()
    print(f"服务在线：档位={info.get('tier')} dim={info.get('embed_dim')}"
          f"{'（dry-run，不实际写入）' if args.dry_run else ''}")

    # 1) 重建分类描述：优先用 dump-categories 的备份，raw_files 目录里备份缺失的用占位补建
    cats = []
    if CATEGORIES_BACKUP.exists():
        cats = json.loads(CATEGORIES_BACKUP.read_text(encoding="utf-8"))
    else:
        print("⚠️ 无 categories_backup.json——分类将以占位描述补建，请事后用 rag_upsert_category 重写。")
    known = {c.get("db_name") for c in cats}
    for d in _category_dirs():
        if d.name not in known and d.name != "unclassified_db":
            cats.append({"db_name": d.name,
                         "description": f"(重建补建，路由描述缺失，请用 rag_upsert_category 重写) {d.name}"})

    for c in cats:
        name, desc = c.get("db_name"), c.get("description", "")
        if not name or name == "unclassified_db":
            continue
        if args.dry_run:
            print(f"[dry-run] 建类 {name}")
            continue
        rr = requests.post(f"{BASE_URL}/api/v1/category", headers=_headers(),
                           json={"db_name": name, "description": desc}, timeout=15)
        rr.raise_for_status()
        print(f"  分类 {name} ✓")

    # 2) 重灌 raw_files（目录名 = 分类），3) 未分类池（自动路由）
    n_docs = 0
    for d in _category_dirs():
        for f in sorted(d.glob("*.txt")):
            _insert_file(f, category=d.name, dry=args.dry_run)
            n_docs += 1
    if UNCLASSIFIED.exists():
        for f in sorted(UNCLASSIFIED.glob("*.txt")):
            _insert_file(f, category=None, dry=args.dry_run)
            n_docs += 1

    verb = "将重灌" if args.dry_run else "已提交重灌"
    print(f"{'[dry-run] ' if args.dry_run else '✅ '}{verb} {n_docs} 篇原文"
          f"（服务端异步切块入库，稍候可检索）。")


def _insert_file(path: Path, category, dry: bool) -> None:
    if dry:
        print(f"[dry-run] insert {path.name} → {category or '(auto-route)'}")
        return
    text = path.read_text(encoding="utf-8")
    # source_path = "备份已在盘上，就地索引"：不带它服务端会另写一份新备份，原文语料每重建
    # 一遍翻一倍（实测 2 篇→4 篇），而 raw_files 正是重建的承重网。见 HETERO_COMPUTE §S.3 B2b。
    body = {"title": _title_from_filename(path), "document": text, "source_path": str(path)}
    if category:
        body["category"] = category
    rr = requests.post(f"{BASE_URL}/api/v1/document/insert", headers=_headers(), json=body, timeout=60)
    rr.raise_for_status()
    print(f"  {path.name} → {category or '(auto-route)'} ✓")


def main() -> None:
    p = argparse.ArgumentParser(
        description="RAG 向量库全量重建（换档/8B 升级）。无子命令时打印换档流程。",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("dump-categories", help="备份分类路由描述（重建前，旧服务在跑时）")
    pw = sub.add_parser("wipe", help="删除 chroma_data 建空库（需先停 rag 服务）")
    pw.add_argument("--yes", action="store_true", help="跳过确认")
    pr = sub.add_parser("rebuild", help="重建分类 + 从 raw_files 全量重灌（新服务在跑时）")
    pr.add_argument("--dry-run", action="store_true", help="只打印计划，不实际写入")

    args = p.parse_args()
    if not args.cmd:
        print(__doc__)
        return
    try:
        {"dump-categories": cmd_dump_categories, "wipe": cmd_wipe, "rebuild": cmd_rebuild}[args.cmd](args)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 401:
            sys.exit("❌ 401 鉴权失败：RAG_API_KEY 与服务端不一致（检查 .env.local 两侧同值）。")
        body = e.response.text[:300] if e.response is not None else ""
        sys.exit(f"❌ 请求失败 HTTP {code}: {body}")


if __name__ == "__main__":
    main()
