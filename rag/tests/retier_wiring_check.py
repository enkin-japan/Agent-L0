#!/usr/bin/env python3
"""换档重建的数据面接线检查（分离部署缺陷一，首评 S10）。

金测试（backend/tests/test_rag_retier.py）用假回调证编排逻辑；本脚本证**真回调**：
_retier_wipe_store / _retier_list_raw_files / _retier_dump_categories /
_retier_reset_and_adopt 对着一个临时数据根上的真 chroma 库跑一整场重建。

不在 devctl 门里（backend venv 无 chromadb）；rag venv 手动跑：
    cd <repo>/rag && .venv/bin/python tests/retier_wiring_check.py
不绑端口、不加载模型（计算面被打桩成固定身份+确定性假向量），随时可跑。
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

RAG = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="retier-wiring-"))

os.environ["RAG_DATA_DIR"] = str(TMP)
os.environ["RAG_COMPUTE_HOST"] = "127.0.0.1"   # 拆分模式；活体身份下面打桩
os.environ.setdefault("RAG_API_KEY", "")

sys.path.insert(0, str(RAG))
spec = importlib.util.spec_from_file_location("rag_server_wiring", RAG / "rag_server.py")
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)

IDENT_OLD = {"tier": "old", "embed_repo": "org/embed-old", "embed_dim": 8, "thresholds": {}}
IDENT_NEW = {"tier": "new", "embed_repo": "org/embed-new", "embed_dim": 4, "thresholds": {}}
LIVE = {"ident": IDENT_OLD}


def fake_embed(texts):
    # 确定性假向量，维度跟随「当前档位」——换档后维度变化是本检查的核心事实
    dim = LIVE["ident"]["embed_dim"]
    return [[float(len(t) % 7 + i) for i in range(dim)] for t in texts]


srv._retier_fetch_live_identity = lambda: dict(LIVE["ident"])
srv.RETIER._fetch_live_identity = srv._retier_fetch_live_identity
srv.COMPUTE.embed = fake_embed
srv.COMPUTE.ensure_ready = lambda: dict(LIVE["ident"])
srv.COMPUTE.token_counts = lambda texts, model_type="embedding": [max(1, len(t) // 2) for t in texts]
srv.get_embeddings = fake_embed


def check(name, ok, detail=""):
    print(("  ✓ " if ok else "  ✗ ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        sys.exit(1)


print("== ① 旧档建库：两个分类 + 三篇原文 ==")
srv._guard_or_stamp_store(IDENT_OLD)                       # 盖旧档戳
srv._retier_upsert_category("faq", "常见问题")
srv._retier_upsert_category("notes", "笔记")
for cat, fname, text in [("faq", "a_1.txt", "问题一的答案内容"),
                         ("faq", "b_2.txt", "问题二的答案内容"),
                         ("notes", "c_3.txt", "一条笔记内容")]:
    d = srv.RAW_FILES_DIR / cat
    d.mkdir(parents=True, exist_ok=True)
    (d / fname).write_text(text, encoding="utf-8")
    srv._retier_insert_document(d / fname, cat)
check("原文清点 = 3 篇", len(srv._retier_list_raw_files()) == 3)
check("dump 出 2 个分类", {c["db_name"] for c in srv._retier_dump_categories()} >= {"faq", "notes"})
check("faq 库有切片", srv.get_chroma_collection("faq").count() > 0)

print("== ② 计算端换档（8 维 → 4 维）→ RETIER 全场重建 ==")
LIVE["ident"] = IDENT_NEW
st = srv.RETIER.status()
check("status 判出 mismatch", st["mismatch"] is True and st["needs_rebuild"] is True)
prog = srv.RETIER.start_rebuild()
import time
deadline = time.time() + 60
while srv.RETIER.progress()["state"] == "running" and time.time() < deadline:
    time.sleep(0.1)
out = srv.RETIER.progress()
check("重建收尾 done", out["state"] == "done", json.dumps(out, ensure_ascii=False))
check("3 篇全重灌", out["docs_done"] == 3 and out["docs_failed"] == 0)

print("== ③ 事后事实核验 ==")
stamp = json.loads(srv.TIER_STAMP_FILE.read_text(encoding="utf-8"))
check("库戳已换新档", stamp["embed_repo"] == "org/embed-new" and stamp["embed_dim"] == 4)
check("耐久标记已撤", not (srv.CHROMA_DB_PATH / ".rebuild_in_progress").exists())
col = srv.get_chroma_collection("faq")
check("faq 切片健在", col.count() > 0)
got = col.get(include=["embeddings"], limit=1)
check("向量已是 4 维", len(got["embeddings"][0]) == 4)
cats2 = {c["db_name"] for c in srv._retier_dump_categories()}
check("路由表重建齐全", cats2 >= {"faq", "notes"})
backup = json.loads((srv.STORAGE_BASE / "categories_backup.json").read_text(encoding="utf-8"))
check("备份文件在且非空", len(backup) >= 2)
st2 = srv.RETIER.status()
check("status 回到同档", st2["mismatch"] is False and st2["needs_rebuild"] is False)
n_raw = len(srv._retier_list_raw_files())
check("原文一篇未动", n_raw == 3)

print(f"== ALL WIRING CHECKS PASS ==（临时数据根 {TMP}，可手动删除）")
