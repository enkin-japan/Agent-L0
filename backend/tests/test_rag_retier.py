"""换档重建管理器金测试（分离部署审计缺陷一，2026-08-31）。

守的不变量（rag/retier.py，devctl 版自动化移除后承接的四道保险）：
① mismatch 判定与换档护栏同口径（embed_repo+embed_dim），无戳/计算面不可达 → None；
② 重建前活体拦截：计算面不可达/身份不完整 → 503 拒；两侧已同档且非 force → 409 拒
   （假成功防线：算力端还没换档就重灌 = 白烧一遍嵌入）；单飞：并发触发 → 409；
③ 受理≠完成：状态机 idle→running→done/failed 如实上报，逐篇计数，单篇失败记账
   不炸整场（done_with_errors）；
④ 事后戳断言：重灌完成后库戳仍与目标身份不符 → failed，不许静默盖过；
⑤ 顺序纪律：分类备份与原文清点先于清库（列不出原文就什么都别删），清库后先
   采纳+盖戳再重建分类。

rag 独立 venv（backend venv 无 chromadb）：retier.py 零三方依赖，按路径装载
（fts_index 测试同惯例）；全部副作用经注入的假回调，可逐步断言调用顺序。
"""
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

RAG_ROOT = Path(__file__).resolve().parents[2] / "rag"

spec = importlib.util.spec_from_file_location("rag_retier_under_test", RAG_ROOT / "retier.py")
retier = importlib.util.module_from_spec(spec)
sys.modules["rag_retier_under_test"] = retier
spec.loader.exec_module(retier)

IDENT_A = {"tier": "text-0.6b", "embed_repo": "org/embed-a", "embed_dim": 1024,
           "thresholds": {"rerank_min_score": 0.5}}
IDENT_B = {"tier": "vl-2b", "embed_repo": "org/embed-b", "embed_dim": 1536}
STAMP_A = {"tier_id": "text-0.6b", "embed_repo": "org/embed-a", "embed_dim": 1024}
STAMP_B = {"tier_id": "vl-2b", "embed_repo": "org/embed-b", "embed_dim": 1536}


# ---------- ① mismatch 同口径 ----------

def test_mismatch_of_same_and_different():
    assert retier.mismatch_of(STAMP_A, IDENT_A) is False
    assert retier.mismatch_of(STAMP_A, IDENT_B) is True
    # 同 repo 不同维度 / 同维度不同 repo：任一不符即 mismatch（护栏同口径）
    assert retier.mismatch_of(STAMP_A, {**IDENT_A, "embed_dim": 2048}) is True
    assert retier.mismatch_of(STAMP_A, {**IDENT_A, "embed_repo": "org/x"}) is True


def test_mismatch_of_undecidable_is_none():
    assert retier.mismatch_of(None, IDENT_A) is None       # 无戳（新库）
    assert retier.mismatch_of(STAMP_A, None) is None       # 计算面不可达
    assert retier.mismatch_of(STAMP_A, {"tier": "?"}) is None  # 身份不完整


def test_mismatch_of_malformed_stamp_is_none_not_raise():
    # 轮询端点的判据（首评 S4）：坏戳退 None，绝不抛异常把状态面炸成 500
    assert retier.mismatch_of({"embed_dim": None}, IDENT_A) is None
    assert retier.mismatch_of({"embed_dim": "abc"}, IDENT_A) is None
    assert retier.mismatch_of(STAMP_A, {"embed_dim": "abc"}) is None


# ---------- 装配一个可编排的假数据面 ----------

class Harness:
    def __init__(self, tmp_path, ident=None, stamp=None,
                 files=None, cats=None):
        self.calls = []
        self.ident = ident
        self.stamp = stamp
        self.files = files if files is not None else [
            (tmp_path / "raw_files/faq/doc1_ab.txt", "faq"),
            (tmp_path / "raw_files/faq/doc2_cd.txt", "faq"),
            (tmp_path / "unclassified_raw/doc3_ef.txt", None)]
        self.cats = cats if cats is not None else [
            {"db_name": "faq", "description": "常见问题"},
            {"db_name": "unclassified_db", "description": "池"}]
        self.insert_fail = set()      # 路径名 → 该篇重灌抛错
        self.upsert_fail = set()      # 分类名 → 路由重建抛错
        self.stale_map = {}           # 过时标记快照（dump_stale 返回）
        self.stale_lost = []          # restore_stale 报告的丢失路径
        self.backup = tmp_path / "storage/categories_backup.json"
        self.marker = tmp_path / "chroma_data/.rebuild_in_progress"
        self.mgr = retier.RetierManager(
            fetch_live_identity=self._fetch, read_stamp=self._read_stamp,
            dump_categories=self._dump, wipe_store=self._wipe,
            reset_and_adopt=self._adopt, upsert_category=self._upsert,
            insert_document=self._insert, list_raw_files=self._list,
            dump_stale=self._dump_stale, restore_stale=self._restore_stale,
            categories_backup=self.backup, rebuild_marker=self.marker)

    def _fetch(self):
        self.calls.append("fetch")
        if self.ident is None:
            raise ConnectionError("compute down")
        return self.ident

    def _read_stamp(self):
        self.calls.append("read_stamp")
        return self.stamp

    def _dump(self):
        self.calls.append("dump")
        return list(self.cats)

    def _wipe(self):
        self.calls.append("wipe")
        assert self.marker.exists(), "耐久标记必须先于第一笔删除落盘（首评 C1）"
        self.stamp = None

    def _adopt(self):
        self.calls.append("adopt")
        self.stamp = {"tier_id": self.ident.get("tier"),   # 正常路：按活体身份重新盖戳
                      "embed_repo": self.ident.get("embed_repo"),
                      "embed_dim": self.ident.get("embed_dim")}

    def _upsert(self, name, desc):
        self.calls.append(f"upsert:{name}")
        if name in self.upsert_fail:
            raise RuntimeError("route embed blew up")

    def _insert(self, path, category):
        self.calls.append(f"insert:{Path(path).name}")
        if Path(path).name in self.insert_fail:
            raise RuntimeError("embed blew up")

    def _list(self):
        self.calls.append("list_files")
        return list(self.files)

    def _dump_stale(self):
        self.calls.append("dump_stale")
        return dict(self.stale_map), 0

    def _restore_stale(self, mapping):
        self.calls.append(f"restore_stale:{len(mapping)}")
        return list(self.stale_lost)

    def run_and_wait(self, force=False, timeout=5.0):
        self.mgr.start_rebuild(force=force)
        deadline = time.time() + timeout
        while self.mgr.progress()["state"] == "running":
            if time.time() > deadline:
                raise AssertionError(f"重建未在 {timeout}s 内收尾: {self.mgr.progress()}")
            time.sleep(0.01)
        return self.mgr.progress()


# ---------- ② 活体拦截 / 假成功防线 / 单飞 ----------

def test_refuses_when_compute_unreachable(tmp_path):
    h = Harness(tmp_path, ident=None, stamp=STAMP_A)
    with pytest.raises(retier.RetierRefused) as ei:
        h.mgr.start_rebuild()
    assert ei.value.code == 503
    assert "wipe" not in h.calls, "计算面不可达时一个字节都不许删"


def test_refuses_incomplete_identity(tmp_path):
    h = Harness(tmp_path, ident={"tier": "?", "embed_repo": "", "embed_dim": None})
    with pytest.raises(retier.RetierRefused) as ei:
        h.mgr.start_rebuild()
    assert ei.value.code == 503


def test_refuses_same_tier_without_force(tmp_path):
    # 假成功防线：算力端还没换档（活体=库戳）→ 拒绝白重灌
    h = Harness(tmp_path, ident=IDENT_A, stamp=STAMP_A)
    with pytest.raises(retier.RetierRefused) as ei:
        h.mgr.start_rebuild()
    assert ei.value.code == 409
    assert "无需重建" in ei.value.detail
    assert "wipe" not in h.calls


def test_force_overrides_same_tier(tmp_path):
    out = Harness(tmp_path, ident=IDENT_A, stamp=STAMP_A).run_and_wait(force=True)
    assert out["state"] == "done"


def test_single_flight_second_start_409(tmp_path):
    # 闸门放在 dump（第一步）：后台线程还没走到 adopt 改戳，第二次 start 的
    # 同档检查必然不命中，确定性落进「已在跑」分支（首评 S14 的竞态修正）
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)
    gate = threading.Event()

    def slow_dump():
        h.calls.append("dump")
        gate.wait(5)
        return list(h.cats)
    h.mgr._dump_categories = slow_dump
    h.mgr.start_rebuild()
    try:
        with pytest.raises(retier.RetierRefused) as ei:
            h.mgr.start_rebuild()
        assert ei.value.code == 409 and "在跑" in ei.value.detail
    finally:
        gate.set()
    deadline = time.time() + 5
    while h.mgr.progress()["state"] == "running" and time.time() < deadline:
        time.sleep(0.01)
    assert h.mgr.progress()["state"] == "done"
    # 单飞释放后可再触发（force：上一轮已把两侧建成同档）
    h.calls.clear()
    assert h.mgr.start_rebuild(force=True)["state"] == "running"
    deadline = time.time() + 5
    while h.mgr.progress()["state"] == "running" and time.time() < deadline:
        time.sleep(0.01)
    assert h.mgr.progress()["state"] == "done"


# ---------- ③ 受理≠完成 / 逐篇记账 ----------

def test_happy_path_counts_and_backup(tmp_path):
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)
    out = h.run_and_wait()
    assert out["state"] == "done"
    assert out["docs_total"] == 3 and out["docs_done"] == 3
    assert out["docs_failed"] == 0 and out["failed_docs"] == []
    assert out["from"] == "org/embed-a" and out["to"] == "org/embed-b"
    cats = json.loads(h.backup.read_text(encoding="utf-8"))
    assert {c["db_name"] for c in cats} == {"faq", "unclassified_db"}
    # unclassified_db 不重建路由条目（它不是真分类）
    assert "upsert:faq" in h.calls and "upsert:unclassified_db" not in h.calls
    assert not h.marker.exists(), "验收通过后耐久标记必须撤掉"


def test_single_doc_failure_is_booked_not_fatal(tmp_path):
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)
    h.insert_fail.add("doc2_cd.txt")
    out = h.run_and_wait()
    assert out["state"] == "done_with_errors"
    assert out["docs_done"] == 3
    assert out["docs_failed"] == 1, "总失败数必须是整数可查（failed_docs 只展示前 20）"
    assert len(out["failed_docs"]) == 1 and "doc2_cd.txt" in out["failed_docs"][0]


def test_category_failure_is_booked_not_fatal(tmp_path):
    # 首评 C2：一条路由描述嵌不出来 ≠ 刚清完库就撂挑子
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)
    h.upsert_fail.add("faq")
    out = h.run_and_wait()
    assert out["state"] == "done_with_errors"
    assert len(out["failed_categories"]) == 1 and "faq" in out["failed_categories"][0]
    assert out["docs_done"] == 3, "路由失败后重灌必须照常进行"


def test_empty_dump_adopts_disk_backup_instead_of_clobbering(tmp_path):
    # 首评 C3：meta 库已损坏（dump 为空）时，盘上好备份是路由描述的最后一份真相
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A, cats=[])
    h.backup.parent.mkdir(parents=True, exist_ok=True)
    h.backup.write_text(json.dumps([{"db_name": "faq", "description": "手写描述"}]),
                        encoding="utf-8")
    out = h.run_and_wait()
    assert out["state"] == "done"
    assert "upsert:faq" in h.calls, "空 dump 必须回落盘上备份重建路由"
    cats = json.loads(h.backup.read_text(encoding="utf-8"))
    assert any(c["db_name"] == "faq" and c["description"] == "手写描述" for c in cats), \
        "好备份不许被空 dump 覆盖"


def test_orphan_category_dir_gets_placeholder_route(tmp_path):
    # 首评 S5：原文目录在、路由条目丢了 → 占位补建，否则重灌成「有数据无路由」的隐形库
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A,
                cats=[{"db_name": "faq", "description": "常见问题"}],
                files=[(tmp_path / "raw_files/faq/a_1.txt", "faq"),
                       (tmp_path / "raw_files/orphan/b_2.txt", "orphan")])
    out = h.run_and_wait()
    assert out["state"] == "done"
    assert "upsert:orphan" in h.calls
    cats = json.loads(h.backup.read_text(encoding="utf-8"))
    orphan = next(c for c in cats if c["db_name"] == "orphan")
    assert "补建" in orphan["description"]


def test_wipe_failure_marks_failed(tmp_path):
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)

    def bad_wipe():
        h.calls.append("wipe")
        raise OSError("disk says no")
    h.mgr._wipe_store = bad_wipe
    out = h.run_and_wait()
    assert out["state"] == "failed" and "disk says no" in out["error"]
    assert not any(c.startswith("insert:") for c in h.calls), "清库失败后不得继续重灌"


# ---------- ④ 事后戳断言 / 耐久标记（首评 C1） ----------

def test_post_stamp_assert_catches_drift(tmp_path):
    # 重灌全程顺利，但收尾时库戳与目标身份不符（重建窗口内计算面又换档）→ failed
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)

    def adopt_wrong():
        h.calls.append("adopt")
        h.stamp = STAMP_A            # 盖回了旧档的戳
    h.mgr._reset_and_adopt = adopt_wrong
    out = h.run_and_wait()
    assert out["state"] == "failed"
    assert "不符" in out["error"]
    assert h.marker.exists(), "失败的重建必须把耐久标记留在盘上"


def test_interrupted_marker_reports_and_unblocks_retry(tmp_path):
    # 中断残局：进程死在重灌半路 → 重启后新进程里 progress=idle、戳是新档（重灌期
    # 嵌入经护栏盖过）、marker 在盘上。status 必须报 interrupted/needs_rebuild，
    # start_rebuild 的同档拒绝必须放行补跑——这正是「假成功」防线的耐久半场。
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_B)   # 两侧同档=单看 mismatch 是健康假象
    h.marker.parent.mkdir(parents=True, exist_ok=True)
    h.marker.write_text('{"to": "org/embed-b"}\n', encoding="utf-8")
    st = h.mgr.status()
    assert st["mismatch"] is False
    assert st["interrupted"] is True and st["needs_rebuild"] is True
    out = h.run_and_wait()          # 不带 force：marker 在场即放行
    assert out["state"] == "done"
    assert not h.marker.exists()
    st2 = h.mgr.status()
    assert st2["interrupted"] is False and st2["needs_rebuild"] is False


def test_thread_start_failure_releases_single_flight(tmp_path, monkeypatch):
    # 首评 S7：线程起不来必须还锁，否则本进程余生全是 409
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)

    class _DeadThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")
    monkeypatch.setattr(retier.threading, "Thread", _DeadThread)
    with pytest.raises(retier.RetierRefused) as ei:
        h.mgr.start_rebuild()
    assert ei.value.code == 500
    monkeypatch.undo()
    assert h.mgr.start_rebuild()["state"] == "running"   # 锁已还，能再来
    deadline = time.time() + 5
    while h.mgr.progress()["state"] == "running" and time.time() < deadline:
        time.sleep(0.01)
    assert h.mgr.progress()["state"] == "done"


def test_fresh_library_without_stamp_can_rebuild(tmp_path):
    # 首评 S15：无戳（新库）不落同档拒绝的短路，可直接重建
    out = Harness(tmp_path, ident=IDENT_B, stamp=None).run_and_wait()
    assert out["state"] == "done"


# ---------- ⑤ 顺序纪律 ----------

def test_backup_and_inventory_precede_wipe(tmp_path):
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)
    h.run_and_wait()
    order = [c for c in h.calls if c in ("dump", "list_files", "wipe", "adopt")]
    assert order.index("dump") < order.index("wipe"), "分类备份必须先于清库"
    assert order.index("list_files") < order.index("wipe"), "原文清点必须先于清库"
    assert order.index("wipe") < order.index("adopt"), "先清库再按活体身份盖新戳"
    first_insert = next(i for i, c in enumerate(h.calls) if c.startswith("insert:"))
    assert h.calls.index("adopt") < first_insert, "重灌必须发生在采纳+盖戳之后"


# ---------- 过时标记保留（阶段复审 m4） / 备份合并（m1） ----------

def test_stale_snapshot_before_wipe_and_restore_after_reinsert(tmp_path):
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)
    h.stale_map = {"/x/a_1.txt": {"stale": "旧合同", "stale_at": "2026-08-01"}}
    out = h.run_and_wait()
    assert out["state"] == "done"
    order = [c for c in h.calls if c in ("dump_stale", "wipe") or c.startswith("restore_stale")]
    assert order[0] == "dump_stale" and order[1] == "wipe", "标记快照必须先于清库"
    assert order[2] == "restore_stale:1", "重灌完成后按快照重挂"
    last_insert = max(i for i, c in enumerate(h.calls) if c.startswith("insert:"))
    assert h.calls.index("restore_stale:1") > last_insert, "重挂必须在全部重灌之后"
    assert out["stale_total"] == 1 and out["stale_restored"] == 1 and out["stale_lost"] == []


def test_stale_lost_paths_are_booked(tmp_path):
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)
    h.stale_map = {"/x/a.txt": {"stale": "r"}, "/x/b.txt": {"stale": "r"}}
    h.stale_lost = ["/x/b.txt"]     # 重灌中被自动路由迁移改了路径
    out = h.run_and_wait()
    assert out["stale_restored"] == 1 and out["stale_lost"] == ["/x/b.txt"]


def test_partial_dump_preserves_handwritten_backup_entries(tmp_path):
    # 阶段复审 m1：dump 部分缺失（如路由被单独删过）时，目录里还有原文的分类
    # 必须沿用旧备份的手写描述——占位补建是没得选时的下策，覆盖手写描述是丢失
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A,
                cats=[{"db_name": "notes", "description": "笔记"}],
                files=[(tmp_path / "raw_files/faq/a_1.txt", "faq"),
                       (tmp_path / "raw_files/notes/b_2.txt", "notes")])
    h.backup.parent.mkdir(parents=True, exist_ok=True)
    h.backup.write_text(json.dumps([{"db_name": "faq", "description": "手写描述"},
                                    {"db_name": "notes", "description": "旧笔记描述"}]),
                        encoding="utf-8")
    out = h.run_and_wait()
    assert out["state"] == "done"
    cats = {c["db_name"]: c["description"] for c in
            json.loads(h.backup.read_text(encoding="utf-8"))}
    assert cats["faq"] == "手写描述", "部分缺失的 dump 不许把手写描述打成占位"
    assert cats["notes"] == "笔记", "dump 里有的以 dump（现役 meta 库）为准"
    assert "upsert:faq" in h.calls and "upsert:notes" in h.calls


def test_backup_file_keeps_orphan_entries_on_disk(tmp_path):
    # 修正评审 C1：重建集（upsert 哪些）与落盘集（写回备份）必须不同——目录已空的
    # 条目不重建（不还魂），但**不许从备份文件里抹掉**（备份是手写描述的最后副本）
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A, cats=[],
                files=[(tmp_path / "raw_files/faq/a_1.txt", "faq")])
    h.backup.parent.mkdir(parents=True, exist_ok=True)
    h.backup.write_text(json.dumps([{"db_name": "faq", "description": "手写A"},
                                    {"db_name": "archive", "description": "手写B"}]),
                        encoding="utf-8")
    out = h.run_and_wait()
    assert out["state"] == "done"
    assert "upsert:faq" in h.calls and "upsert:archive" not in h.calls
    disk = {c["db_name"]: c["description"] for c in
            json.loads(h.backup.read_text(encoding="utf-8"))}
    assert disk.get("archive") == "手写B", "落盘备份不许抹掉孤儿条目的手写描述"
    assert disk.get("faq") == "手写A"


def test_malformed_backup_does_not_block_rebuild(tmp_path):
    # 修正评审 S：坏备份文件（非 list / 条目非 dict）按空处理，不许挡住重建
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)
    h.backup.parent.mkdir(parents=True, exist_ok=True)
    h.backup.write_text('{"not": "a list"}', encoding="utf-8")
    assert h.run_and_wait()["state"] == "done"
    h2 = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)
    h2.backup.write_text('["字符串条目", 42]', encoding="utf-8")
    assert h2.run_and_wait()["state"] == "done"


def test_stale_dump_failure_is_booked_as_errors(tmp_path):
    # 修正评审 S：单库快照失败=该库标记静默丢失——必须计数上报并降级 done_with_errors
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)
    h.mgr._dump_stale = lambda: ({}, 2)
    out = h.run_and_wait()
    assert out["state"] == "done_with_errors"
    assert out["stale_dump_failed"] == 2


def test_deleted_category_with_no_files_stays_deleted(tmp_path):
    # 用户真删（路由删了、原文也清了）的分类不复活
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A,
                cats=[{"db_name": "faq", "description": "常见问题"}],
                files=[(tmp_path / "raw_files/faq/a_1.txt", "faq")])
    h.backup.parent.mkdir(parents=True, exist_ok=True)
    h.backup.write_text(json.dumps([{"db_name": "gone", "description": "已删"}]),
                        encoding="utf-8")
    h.run_and_wait()
    assert "upsert:gone" not in h.calls


# ---------- 状态面 ----------

def test_status_reports_mismatch_and_compute_error(tmp_path):
    h = Harness(tmp_path, ident=IDENT_B, stamp=STAMP_A)
    st = h.mgr.status()
    assert st["mismatch"] is True
    assert st["store"]["embed_repo"] == "org/embed-a"
    assert st["compute"]["embed_repo"] == "org/embed-b"
    assert st["rebuild"]["state"] == "idle"

    h2 = Harness(tmp_path, ident=None, stamp=STAMP_A)
    st2 = h2.mgr.status()
    assert st2["mismatch"] is None and st2["compute"] is None
    assert "compute down" in st2["compute_error"]
