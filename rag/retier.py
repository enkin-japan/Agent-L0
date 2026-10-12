"""换档重建管理器（分离部署审计缺陷一，2026-08-31）。

执行端驱动的正式口径：计算端换嵌入档位后，**数据面自己**提供受鉴权的
状态/重建端点（rag_server 装配），app/面板据此检测「计算面档位 ≠ 库内档位戳」、
提示用户、确认后由服务端就地重建——执行端只需要打包好的 app，不需要 repo
（devctl 版自动化已按用户裁决整个移除，本模块承接它的四道保险）：

  ① 护栏同口径比对：mismatch 判定与 _guard_or_stamp_store 完全同键
     （embed_repo + embed_dim），不另立标准；
  ② 重灌前活体拦截：重建开跑前**绕过缓存**现查计算面身份（拆分模式的
     _RemoteCompute 会把首次错档缓存成 fatal、把身份缓存成旧值——重建恰恰
     发生在这两个缓存都不可信的时刻）；两侧已同档时拒绝重建（force 除外），
     防「算力端还没换档就白重灌一遍」的假成功；
  ③ 受理≠完成的治理：重建在后台线程**同步逐篇**处理并计数，状态机
     idle→running→done/failed 由 GET 端点如实上报——「提交了」不冒充「建完了」；
  ④ 事后戳断言：全部重灌完成后再核一次库戳与计算面身份，不符即 failed
     （中途计算面又换档/重启的窗口不许静默盖过）。

安全（与 purge 端点同纪律）：wipe 只清向量库与 FTS 索引（派生物），原文
storage/raw_files 是唯一真相、一个字节不动；分类路由描述与过时标记分别耐久备份
categories_backup.json / stale_backup.json 再清库。

本模块**零第三方依赖**（backend venv 无 chromadb 也可金测试）：chroma/FTS/
计算面/入库流水线全部由 rag_server 注入。
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional


def mismatch_of(stamp: Optional[dict], ident: Optional[dict]) -> Optional[bool]:
    """库戳 vs 计算面身份，与 _guard_or_stamp_store 同口径（embed_repo+embed_dim）。
    返回 None = 判定不了（无戳=新库随首次请求盖戳，或计算面不可达，或**任一侧维度
    字段畸形**——本函数是轮询端点的判据，坏戳退 None，绝不抛异常把状态面炸成 500）。"""
    if not stamp or not ident:
        return None
    if ident.get("embed_dim") is None:
        return None
    try:
        dim_differs = int(stamp.get("embed_dim", -1)) != int(ident["embed_dim"])
    except (TypeError, ValueError):
        return None
    return dim_differs or stamp.get("embed_repo") != ident.get("embed_repo")


class RetierManager:
    """状态查询 + 单飞后台重建。全部副作用经注入的回调进行。

    注入面（rag_server 装配）：
      fetch_live_identity() -> dict            绕缓存现查计算面身份（不可达则 raise）
      read_stamp() -> Optional[dict]           读库戳（无/坏 → None）
      dump_categories() -> list[dict]          读 meta 库路由描述 [{db_name, description}]
      wipe_store() -> None                     清全部 collection + FTS + 库戳（原文不动）
      reset_and_adopt() -> None                清计算面缓存（拆分模式的 identity/_fatal），
                                               按活体身份给空库重新盖戳并套用档位阈值
      upsert_category(name, desc) -> None      重建一条分类路由（内部走新档位嵌入）
      insert_document(path: Path, category: Optional[str]) -> None
                                               按原文路径就地重灌一篇（同步，完成才返回）
      list_raw_files() -> list[(Path, Optional[str])]
                                               全部原文 [(路径, 分类或 None=自动路由)]
      dump_stale() -> (dict[str, dict], int)   过时标记快照 {source_path: {stale,
                                               stale_at}} + 快照失败的库数（阶段复审
                                               m4：治理元数据只活在切片 metadata 里，
                                               不抢救就随 wipe 归零；哪个库快照失败
                                               要计数上报并拒绝清库，静默丢标记正是
                                               m4 / R14 要防的）
      restore_stale(mapping) -> list[str]      重灌后按 source_path 重挂标记；返回没
                                               找到切片的路径（重灌中被自动路由迁移
                                               改了路径、或该篇重灌失败）
    rebuild_marker：耐久的「重建进行中」标记文件（首评 C1）。清库前落盘、验收通过
    才删——重建中途进程死掉后，库是空的而新戳已在（重灌期的嵌入会经护栏无戳分支
    盖戳），单看 mismatch 是「健康」的假象；标记在盘上，重启后的 status() 才能报
    interrupted / needs_rebuild，app 才会再次提示补跑。
    """

    def __init__(self, *, fetch_live_identity: Callable[[], dict],
                 read_stamp: Callable[[], Optional[dict]],
                 dump_categories: Callable[[], List[dict]],
                 wipe_store: Callable[[], None],
                 reset_and_adopt: Callable[[], None],
                 upsert_category: Callable[[str, str], None],
                 insert_document: Callable[[Path, Optional[str]], None],
                 list_raw_files: Callable[[], List[tuple]],
                 dump_stale: Callable[[], dict],
                 restore_stale: Callable[[dict], List[str]],
                 categories_backup: Path,
                 rebuild_marker: Path,
                 stale_backup: Optional[Path] = None,
                 write_drain_timeout: float = 300.0,
                 logger=None):
        self._fetch_live_identity = fetch_live_identity
        self._read_stamp = read_stamp
        self._dump_categories = dump_categories
        self._wipe_store = wipe_store
        self._reset_and_adopt = reset_and_adopt
        self._upsert_category = upsert_category
        self._insert_document = insert_document
        self._list_raw_files = list_raw_files
        self._dump_stale = dump_stale
        self._restore_stale = restore_stale
        self._categories_backup = Path(categories_backup)
        self._rebuild_marker = Path(rebuild_marker)
        self._stale_backup = Path(stale_backup or self._categories_backup.with_name("stale_backup.json"))
        self._log = logger
        self._lock = threading.Lock()          # 单飞：任一时刻至多一个重建
        self._state_lock = threading.Lock()    # progress 读写互斥（GET 与后台线程并发）
        self._progress = {"state": "idle"}
        # Reserve at HTTP acceptance, before FastAPI starts BackgroundTasks. A running
        # task and an accepted-but-delayed task both own one ticket until completion.
        self._writes = threading.Condition()
        self._accepted_writes = set()
        self._accepting_writes = True
        # BackgroundTasks may never start if sending its response is cancelled.
        # Bound the wait and fail before inventory/wipe; never bypass the drain.
        self._write_drain_timeout = write_drain_timeout
        self._recovery_lock = threading.Lock()  # serialize edits to the existing backup

    def acquire_write(self, op: str):
        with self._writes:
            if not self._accepting_writes or self.progress().get("state") == "running":
                raise RetierRefused(409, f"知识库换档重建进行中，暂不受理{op}——请稍后再来。")
            ticket = object()
            self._accepted_writes.add(ticket)
            self._set(writes_pending=len(self._accepted_writes))
            return ticket

    def release_write(self, ticket) -> None:
        with self._writes:
            self._accepted_writes.discard(ticket)
            self._set(writes_pending=len(self._accepted_writes))
            self._writes.notify_all()

    # ---------------- 状态面 ----------------

    def progress(self) -> dict:
        with self._state_lock:
            # 浅拷贝即安全：后台线程对 failed_docs 只整体替换（failed[:20]）不就地
            # append——将来谁改成 append 就悄悄打穿这个快照，别改（首评 nit）
            return dict(self._progress)

    def _interrupted(self) -> bool:
        """耐久标记在、且本进程没有在跑的重建 = 上一场重建中途死了（含进程重启）。"""
        return self._rebuild_marker.exists() and self.progress().get("state") != "running"

    def status(self) -> dict:
        """app/面板的检测数据源：库戳、计算面活体身份、同口径 mismatch 判定、重建进度。
        needs_rebuild 是 app 的唯一提示判据：真换档（mismatch）与中断残局（interrupted）
        都要把用户拉回来补跑。"""
        stamp = self._read_stamp()
        ident, err = None, None
        try:
            ident = self._fetch_live_identity()
        except Exception as e:
            err = str(e)
        interrupted = self._interrupted()
        mismatch = mismatch_of(stamp, ident)
        out = {
            "store": ({"tier": stamp.get("tier_id"), "embed_repo": stamp.get("embed_repo"),
                       "embed_dim": stamp.get("embed_dim")} if stamp else None),
            "compute": ({"tier": ident.get("tier"), "embed_repo": ident.get("embed_repo"),
                         "embed_dim": ident.get("embed_dim")} if ident else None),
            "mismatch": mismatch,
            "interrupted": interrupted,
            "needs_rebuild": mismatch is True or interrupted,
            "rebuild": self.progress(),
        }
        if err:
            out["compute_error"] = err
        return out

    # ---------------- 重建 ----------------

    def start_rebuild(self, force: bool = False) -> dict:
        """校验后启动后台重建线程。冲突/无必要/计算面不可达按 RetierRefused 上抛，
        由端点层翻译成 409/503——绝不静默吞掉再假装开跑。"""
        ident = None
        try:
            ident = self._fetch_live_identity()
        except Exception as e:
            raise RetierRefused(503, f"计算面不可达，无法重建（重灌要靠它嵌入）：{e}")
        try:
            dim_ok = int(ident.get("embed_dim")) > 0
        except (TypeError, ValueError):
            dim_ok = False
        if not dim_ok or not ident.get("embed_repo"):
            raise RetierRefused(503, f"计算面身份不完整（{ident!r}），拒绝按它重建")
        stamp = self._read_stamp()
        # 同档拒绝（假成功防线）对中断残局不适用：上一场重建死在半路时戳已是新档
        # （mismatch=False），但库是残的——耐久标记在就放行补跑（首评 C1）。
        if (stamp is not None and mismatch_of(stamp, ident) is False
                and not force and not self._rebuild_marker.exists()):
            raise RetierRefused(
                409, f"两侧已同档（{ident.get('tier')} / dim={ident.get('embed_dim')}），"
                     f"无需重建。修库场景请显式传 force=true。")
        if not self._lock.acquire(blocking=False):
            raise RetierRefused(409, "已有重建在跑（GET 本端点看进度）")
        with self._writes:
            self._accepting_writes = False
        with self._state_lock:
            self._progress = {
                "state": "running", "phase": "starting",
                "docs_total": None, "docs_done": 0, "docs_failed": 0,
                "failed_docs": [], "failed_categories": [],
                # stale 三件套恒初始化（修正评审 nit）：状态形状不随「有没有标记」漂移；
                # stale_lost 同 failed_docs 只展示前 20，总量 = stale_total - stale_restored
                "stale_total": 0, "stale_restored": 0, "stale_lost": [],
                "stale_dump_failed": 0,
                "from": (stamp or {}).get("embed_repo"),
                "to": ident.get("embed_repo"),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        try:
            t = threading.Thread(target=self._run, args=(ident,), name="rag-retier", daemon=True)
            t.start()
        except Exception as e:
            # 线程都起不来：单飞锁必须当场还回去，否则本进程余生全是 409（首评 S7）
            self._set(state="failed", error=f"重建线程启动失败：{e}")
            with self._writes:
                self._accepting_writes = True
            self._lock.release()
            raise RetierRefused(500, f"重建线程启动失败：{e}")
        return self.progress()

    def _set(self, **kw) -> None:
        with self._state_lock:
            self._progress.update(kw)

    @staticmethod
    def _atomic_json(path: Path, data) -> None:
        """Persist before destructive work: file fsync, atomic replace, directory fsync."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=path.name + ".", delete=False) as f:
                tmp = Path(f.name)
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(path)
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    @staticmethod
    def raw_identity(path: Path) -> Optional[str]:
        """Hard-link moves preserve this identity; path reuse/content changes do not."""
        try:
            stat = path.stat()
            if not path.is_file():
                return None
            return f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            return None

    def _read_stale_recovery(self) -> dict:
        if not self._rebuild_marker.exists() or not self._stale_backup.exists():
            return {}
        saved = json.loads(self._stale_backup.read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or any(not isinstance(v, dict) for v in saved.values()):
            raise RuntimeError("过时标记耐久备份损坏，拒绝清库；请保留备份并修复后重试")
        # Keys remain stable recovery slots. Never collapse two generations onto a
        # move destination: a deleted document and its new occupant are different.
        return saved

    def _record_path(self, key: str, meta: dict) -> Optional[str]:
        source = meta.get("_source_path", key)
        identity = meta.get("_raw_id")
        if not identity:
            if meta.get("_deleted") or meta.get("_moved_to"):
                raise RuntimeError(f"原文操作缺少身份，无法安全认领恢复资料: {source}")
            identity = self.raw_identity(Path(source))
            if not identity:
                raise RuntimeError(f"原文缺失，无法建立恢复身份，拒绝清库: {source}")
            meta["_raw_id"] = identity
        if self.raw_identity(Path(source)) == identity:
            return source
        moved = meta.get("_moved_to")
        if moved and self.raw_identity(Path(moved)) == identity:
            return moved
        if meta.get("_deleted"):
            return None
        raise RuntimeError(f"原文身份变化或无法确认迁移，恢复资料已保留: {source}")

    def _find_record(self, saved: dict, source: str, *, missing: bool = False):
        identity = self.raw_identity(Path(source))
        if identity:
            for key, meta in saved.items():
                active = self._record_path(key, meta) if meta.get("_raw_id") == identity else None
                if active is not None and Path(active).resolve() == Path(source).resolve():
                    return key
            for key, meta in saved.items():
                origin = meta.get("_source_path", key)
                if (not meta.get("_raw_id") and not meta.get("_deleted")
                        and not meta.get("_moved_to") and Path(origin).resolve() == Path(source).resolve()):
                    return key
        elif missing:
            for key, meta in saved.items():
                if key == source or meta.get("_source_path", key) == source:
                    return key
        return None

    def _new_record(self, saved: dict, source: str, meta: dict):
        identity = self.raw_identity(Path(source))
        key = source if source not in saved else f"{source}#raw={identity}"
        if key in saved:
            raise RuntimeError(f"恢复记录身份冲突，拒绝覆盖: {source}")
        saved[key] = {**meta, "_source_path": source}
        if identity:
            saved[key]["_raw_id"] = identity
        return key

    def record_stale_change(self, source: str, reason: str, stale_at: str,
                            *, live_matches: bool) -> bool:
        with self._recovery_lock:
            if not self._rebuild_marker.exists():
                return False
            saved = self._read_stale_recovery()
            key = self._find_record(saved, source, missing=True)
            if key is None:
                if not live_matches:
                    return False
                key = self._new_record(saved, source, {})
            if saved[key].get("_deleted") and self._record_path(key, saved[key]) is None:
                return False
            # Reason changes retain the entire in-flight removal/move identity.
            saved[key].update(stale=reason, stale_at=stale_at, _override=True)
            if not saved[key].get("_raw_id"):
                identity = self.raw_identity(Path(source))
                if identity:
                    saved[key]["_raw_id"] = identity
            self._atomic_json(self._stale_backup, saved)
            return True

    def record_raw_removal(self, path: Path, *, directory: bool = False) -> None:
        with self._recovery_lock:
            saved = self._read_stale_recovery()
            changed = False
            for key, meta in saved.items():
                source = self._record_path(key, meta)
                if source is None:
                    continue
                source_path = Path(source).resolve()
                target = path.resolve()
                if source_path == target or (directory and source_path.is_relative_to(target)):
                    identity = meta.get("_raw_id") or self.raw_identity(Path(source))
                    if not identity:
                        raise RuntimeError(f"无法确认待删除原文身份，恢复资料已保留: {source}")
                    meta.update(_deleted=True, _override=True, _raw_id=identity, _source_path=source)
                    meta.pop("_moved_to", None)
                    changed = True
            if changed:
                self._atomic_json(self._stale_backup, saved)

    def record_raw_move(self, source: Path, destination: Path) -> None:
        with self._recovery_lock:
            saved = self._read_stale_recovery()
            # A legacy missing original must not acquire the identity of an
            # incoming document before the next rebuild's pre-wipe validation.
            for old_key, meta in saved.items():
                origin = Path(meta.get("_source_path", old_key))
                if not meta.get("_raw_id") and origin.resolve() == destination.resolve():
                    self._record_path(old_key, meta)
            key = self._find_record(saved, str(source))
            if key is not None:
                identity = saved[key].get("_raw_id") or self.raw_identity(source)
                if not identity:
                    raise RuntimeError(f"无法确认待迁移原文身份，恢复资料已保留: {source}")
                saved[key].update(_source_path=str(source), _moved_to=str(destination), _raw_id=identity)
                saved[key].pop("_deleted", None)
                self._atomic_json(self._stale_backup, saved)

    def _restore_mapping(self, saved: dict) -> dict:
        out = {}
        for key, meta in saved.items():
            source = self._record_path(key, meta)
            if source is None:
                continue
            if source in out:
                raise RuntimeError(f"多条恢复记录指向同一原文，拒绝猜测归属: {source}")
            out[source] = {"stale": meta.get("stale", ""), "stale_at": meta.get("stale_at", "")}
            if meta.get("_raw_id"):
                out[source]["_raw_id"] = meta["_raw_id"]
        return out

    def _backup_stale(self, current: dict) -> dict:
        with self._recovery_lock:
            saved = self._read_stale_recovery()
            for source, meta in current.items():
                key = self._find_record(saved, source, missing=True)
                if key is None:
                    self._new_record(saved, source, meta)
                elif not saved[key].get("_raw_id") and not saved[key].get("_deleted") and not saved[key].get("_moved_to"):
                    identity = self.raw_identity(Path(source))
                    if identity:
                        saved[key]["_raw_id"] = identity
                # Existing recovery records are authoritative while interrupted:
                # governance writes update them before live mutations. Old chunks
                # at a reused path carry no generation proof and cannot override.
            mapping = self._restore_mapping(saved)
            self._atomic_json(self._stale_backup, saved)
            return mapping

    def _backup_categories(self, cats: List[dict], files: List[tuple]) -> List[dict]:
        """备份分类路由描述并回填缺口。防线（首评 C3/S5 + 阶段复审 m1 + 修正评审 C1）：
        - 路由描述是用户手写的、原文里没有——覆盖/删除 = 永久丢失。**重建集**（返回
          值，决定 upsert 哪些路由）与**落盘集**（写回备份文件）刻意不同：
          · 重建集 = dump（现役 meta 库，同名胜出）+ 旧备份里目录仍有原文的条目
            （沿用手写描述，胜过占位）+ 两边都无的原文目录占位；目录已空的不复活
            （用户真删的路由不还魂）；
          · 落盘集 = 重建集 ∪ 旧备份**全部**条目——备份是描述的最后一份真相，
            孤儿条目留在备份里无害，从备份里抹掉才是不可逆（修正评审 C1：曾把
            落盘集写成重建集，meta 库损坏时手写描述被静默清出最后副本）。
        - 旧备份形状畸形（非 list / 条目非 dict）按空处理，不许一个坏文件挡住重建
          （方向安全：解析失败只是少了回填来源，dump 仍在）；
        - 写备份走临时文件 + 原子改名，写一半断电不会留下半个 JSON。"""
        try:
            old = json.loads(self._categories_backup.read_text(encoding="utf-8"))
        except Exception:
            old = []
        if not isinstance(old, list):
            old = []
        old = [c for c in old if isinstance(c, dict) and c.get("db_name")]
        file_cats = {c for _, c in files if c}
        known = {c.get("db_name") for c in cats if c.get("db_name")}
        for c in old:
            name = c["db_name"]
            if name not in known and name in file_cats:
                if self._log:
                    self._log.warning(f"⚠️ 分类 {name} 不在 meta 路由库但原文尚存："
                                      f"沿用盘上备份的路由描述重建")
                cats.append(c)
                known.add(name)
        for cat_name in sorted(file_cats - known):
            cats.append({"db_name": cat_name,
                         "description": f"(重建补建，路由描述缺失，请用 rag_upsert_category 重写) {cat_name}"})
        to_disk = {c["db_name"]: c for c in old}
        to_disk.update({c.get("db_name"): c for c in cats if c.get("db_name")})
        if to_disk:
            self._atomic_json(self._categories_backup, list(to_disk.values()))
        return cats

    def _run(self, ident: dict) -> None:
        try:
            with self._writes:
                self._set(phase="drain_writes", writes_pending=len(self._accepted_writes))
                drained = self._writes.wait_for(lambda: not self._accepted_writes,
                                               timeout=self._write_drain_timeout)
                if not drained:
                    raise RuntimeError(f"等待已受理写入完成超时（{len(self._accepted_writes)} 项），"
                                       "未清点/清库；请待写入完成后重试，未启动任务可重启恢复")
            # 1) 备份分类路由描述（清库会连 meta 库一起清）+ 原文清点。
            #    两者都先于清库：备份不出来/列不出原文，就什么都别删。
            self._set(phase="dump_categories")
            files = self._list_raw_files()
            cats = self._backup_categories(self._dump_categories(), files)
            # 过时标记快照（阶段复审 m4）：治理元数据只活在切片 metadata 里，
            # 与分类备份同理必须先于清库抢救出来。快照整体失败会中止重建（此时
            # 什么都还没删，方向安全）；单库失败也中止，不把不完整快照当成功。
            # 快照耐久落盘后才允许 wipe；中断后必须沿用旧快照恢复缺失元数据。
            stale_map, stale_dump_failed = self._dump_stale()
            self._set(stale_dump_failed=stale_dump_failed)
            if stale_dump_failed:
                raise RuntimeError(f"过时标记快照失败（{stale_dump_failed} 个库），未清库，请重试")
            stale_map = self._backup_stale(stale_map)
            self._set(docs_total=len(files), stale_total=len(stale_map),
                      stale_dump_failed=stale_dump_failed)

            # 2) 落耐久标记（首评 C1）→ 清库（向量 + FTS + 戳；原文不动）→ 清计算面
            #    缓存并按活体身份重新盖戳+套用档位阈值。标记必须先于第一笔删除：从这
            #    一刻起库处于「重建中」，进程死在任何一步，重启后 status() 都会报
            #    interrupted——新戳在场 + 库是空的 ≠ 健康。
            self._set(phase="wipe")
            self._atomic_json(self._rebuild_marker, {
                "to": ident.get("embed_repo"),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            })
            self._wipe_store()
            self._reset_and_adopt()

            # 3) 重建分类路由（新档位嵌入）。逐条记账不炸整场（首评 C2）：一条描述
            #    嵌不出来就中止 = 刚清完库就撂挑子，比缺一条路由糟得多。
            self._set(phase="categories")
            failed_cats = []
            for c in cats:
                name = c.get("db_name")
                if not name or name == "unclassified_db":
                    continue
                try:
                    self._upsert_category(name, c.get("description", ""))
                except Exception as e:
                    failed_cats.append(f"{name}: {e}")
                    if self._log:
                        self._log.warning(f"⚠️ 分类路由重建失败（继续）: {name}: {e}")
            self._set(failed_categories=failed_cats[:20])

            # 4) 同步逐篇重灌（受理=完成；失败逐篇记账，不炸整场——大语料中途
            #    坏一篇就放弃等于把库留在半重建态，比带着少数缺篇收尾更糟）。
            #    docs_done = 已处理数（进度条语义），docs_failed 单列（首评 S8：
            #    failed_docs 只展示前 20 条，总数必须另有整数可查）。
            self._set(phase="reinsert")
            failed = []
            done = 0
            for path, category in files:
                try:
                    self._insert_document(Path(path), category)
                except Exception as e:
                    failed.append(f"{Path(path).name}: {e}")
                    if self._log:
                        self._log.warning(f"⚠️ 重灌失败（继续下一篇）: {path}: {e}")
                done += 1
                self._set(docs_done=done, docs_failed=len(failed),
                          failed_docs=failed[:20])

            # 5) 重挂过时标记：按原 source_path 匹配新切片。匹配不到的（重灌中被
            #    自动路由迁移改了路径 / 该篇重灌失败）逐条记账——标记丢失要可见，
            #    元数据失败保留耐久备份/标记并报 failed，重试不会丢掉旧快照。
            if stale_map:
                self._set(phase="restore_stale")
                # Reinsertion may have moved a raw file. Re-read exact durable
                # identities/targets instead of guessing its new path by basename.
                with self._recovery_lock:
                    stale_map = self._restore_mapping(self._read_stale_recovery())
                self._set(stale_total=len(stale_map))
                try:
                    lost = self._restore_stale(stale_map)
                except Exception as e:
                    lost = sorted(stale_map)
                    if self._log:
                        self._log.warning(f"⚠️ 过时标记整批重挂失败（耐久备份保留，原文无恙）: {e}")
                self._set(stale_restored=len(stale_map) - len(lost),
                          stale_lost=sorted(lost)[:20])
                if lost:
                    raise RuntimeError(f"过时标记重挂失败（{len(lost)} 篇），耐久备份已保留，请重试")

            # 6) 事后戳断言：库戳必须已按活体身份重盖（保险④）
            self._set(phase="verify")
            stamp = self._read_stamp()
            if mismatch_of(stamp, ident) is not False:
                raise RuntimeError(
                    f"重建后库戳与目标身份不符（戳={stamp!r} 目标="
                    f"{ident.get('embed_repo')}/dim={ident.get('embed_dim')}）——"
                    f"重建期间计算面可能又换档/重启，请核实后重跑")
            # 验收过了才撤耐久标记；失败/中断都把它留在盘上，app 会再来提示补跑
            self._rebuild_marker.unlink(missing_ok=True)
            clean = not (failed or failed_cats
                         or self.progress().get("stale_dump_failed")
                         or self.progress().get("stale_lost"))
            state = "done" if clean else "done_with_errors"
            self._set(state=state, phase="idle",
                      finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            if self._log:
                self._log.info(f"✅ 换档重建完成：{done - len(failed)}/{done} 篇入新库"
                               f"（档位 {ident.get('tier')} / dim={ident.get('embed_dim')}）")
        except Exception as e:
            self._set(state="failed", error=str(e),
                      finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            if self._log:
                self._log.error(f"❌ 换档重建失败：{e}")
        finally:
            with self._writes:
                self._accepting_writes = True
            self._lock.release()


class RetierRefused(Exception):
    """start_rebuild 的校验拒绝：code 供端点层映射 HTTP 状态（409/503）。"""

    def __init__(self, code: int, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail
