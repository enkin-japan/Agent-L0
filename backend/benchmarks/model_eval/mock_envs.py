"""长链维度的确定性 mock 工具环境（tau-bench 思路：可校验终态 + 脚本化世界）。

评的是多步工具链推理：读→算→写、错误恢复、不多做不漏做。环境是纯内存状态机，
handler 全部确定性——同一模型行为必得同一终态，可复现可回归。错误路径给真实
风格的错误回执（找不到文件等），模型的错误恢复行为也是测量面。

环境注册：MOCK_ENVS = {"vfs": 虚拟文件系统, "todo": 待办清单, "office": 二者合一}。
题面用法：task.env = "vfs"，task.env_init = 初始状态；终态断言见 scorers/longchain。

env_init 的两个**确定性故障面**（vfs/office 共用，缺省不启用、行为与从前逐字节一致）：
  faults   {工具名: {"fail_first_n": N, "error": "..."}}——该工具前 N 次调用返回
           错误回执且**不改状态**，第 N+1 次起正常。测瞬时故障后的重试纪律
           （longchain 打分器的 recovered：出过环境错误但终态仍全对）。
  readonly ["path", ...]——这些路径上的 write/append/delete 永远 EACCES。测的是
           「撞墙后改道」而不是「反复砸同一扇门」（redundant_calls 会记账）。
"""
import json
from typing import Any, Dict, List, Optional


def take_fault(faults: Dict[str, Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """消费一次故障配额：该工具还欠 N 次失败则返回错误回执并把 N 减一，否则 None。

    计数记在 faults 表里（环境实例私有），所以同一道题重复跑必得同一故障序列——
    随机故障会让终态断言的红/绿不可复现，那就不是可回归的测量了。
    """
    spec = faults.get(name)
    if not spec:
        return None
    left = int(spec.get("fail_first_n") or 0)
    if left <= 0:
        return None
    spec["fail_first_n"] = left - 1
    return {"error": str(spec.get("error")
                         or "EBUSY: resource temporarily busy, retry")}


class VfsEnv:
    """虚拟文件系统：list_files / read_file / write_file / append_file / delete_file。"""

    def __init__(self, init: Dict[str, Any] = None):
        init = init or {}
        self.files: Dict[str, str] = dict(init.get("files") or {})
        self.faults: Dict[str, Dict[str, Any]] = {
            k: dict(v) for k, v in (init.get("faults") or {}).items()}
        self.readonly = set(init.get("readonly") or [])

    def tools(self) -> List[Dict[str, Any]]:
        def t(name, desc, props, req):
            return {"name": name, "description": desc,
                    "parameters": {"type": "object", "properties": props,
                                   "required": req}}
        path = {"path": {"type": "string"}}
        return [
            t("list_files", "List all file paths.", {}, []),
            t("read_file", "Read a file's content.", dict(path), ["path"]),
            t("write_file", "Create or overwrite a file.",
              {**path, "content": {"type": "string"}}, ["path", "content"]),
            t("append_file", "Append to an existing file.",
              {**path, "content": {"type": "string"}}, ["path", "content"]),
            t("delete_file", "Delete a file.", dict(path), ["path"]),
        ]

    def handle(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        path = str(args.get("path", ""))
        # 先做参数/只读/存在性校验，**本来就会失败的调用不消耗故障配额**——否则故障
        # 落点会随模型自己的失误漂移（评审 #20），"前 N 次调用失败"就不再是确定的。
        if path in self.readonly and name in ("write_file", "append_file", "delete_file"):
            return {"error": f"EACCES: read-only file: {path}"}
        if name == "list_files":
            return self._with_fault(name, lambda: {"files": sorted(self.files)})
        if name == "read_file":
            if path not in self.files:
                return {"error": f"File not found: {path}"}
            return self._with_fault(name, lambda: {"path": path, "content": self.files[path]})
        if name == "write_file":
            if "content" not in args:
                return {"error": "Missing required parameter: content"}
            def _write():
                self.files[path] = str(args["content"])
                return {"status": "written", "path": path}
            return self._with_fault(name, _write)
        if name == "append_file":
            if path not in self.files:
                return {"error": f"File not found: {path} (use write_file to create)"}
            def _append():
                self.files[path] += str(args.get("content", ""))
                return {"status": "appended", "path": path}
            return self._with_fault(name, _append)
        if name == "delete_file":
            if path not in self.files:
                return {"error": f"File not found: {path}"}
            def _delete():
                del self.files[path]
                return {"status": "deleted", "path": path}
            return self._with_fault(name, _delete)
        return {"error": f"Unknown tool: {name}"}

    def _with_fault(self, name: str, action):
        """校验全过之后才问故障表：欠着失败次数就返回错误回执且**不执行** action。"""
        injected = take_fault(self.faults, name)
        if injected is not None:
            return injected            # 瞬时故障：不改状态，重试即可通过
        return action()

    def snapshot(self) -> Dict[str, Any]:
        return {"files": dict(self.files)}


class TodoEnv:
    """待办清单：todo_add / todo_list / todo_complete（按文本匹配，避免索引脆弱）。"""

    def __init__(self, init: Dict[str, Any] = None):
        init = init or {}
        self.todos: List[Dict[str, Any]] = [dict(t) for t in init.get("todos") or []]
        # 与 VfsEnv 同一套故障面（office 环境的 env_init.faults 对两类工具都有效）
        self.faults: Dict[str, Dict[str, Any]] = {
            k: dict(v) for k, v in (init.get("faults") or {}).items()
            if k in ("todo_add", "todo_list", "todo_complete")}

    def tools(self) -> List[Dict[str, Any]]:
        return [
            {"name": "todo_add", "description": "Add a todo item.",
             "parameters": {"type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"]}},
            {"name": "todo_list", "description": "List all todos with status.",
             "parameters": {"type": "object", "properties": {}, "required": []}},
            {"name": "todo_complete", "description": "Mark a todo as done by its text.",
             "parameters": {"type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"]}},
        ]

    def handle(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        text = str(args.get("text", ""))
        if name == "todo_add":
            if not text:
                return {"error": "Missing required parameter: text"}
            injected = take_fault(self.faults, name)
            if injected is not None:
                return injected
            self.todos.append({"text": text, "done": False})
            return {"status": "added", "text": text}
        if name == "todo_list":
            injected = take_fault(self.faults, name)
            if injected is not None:
                return injected
            return {"todos": [dict(t) for t in self.todos]}
        if name == "todo_complete":
            for t in self.todos:
                if t["text"] == text:
                    injected = take_fault(self.faults, name)
                    if injected is not None:
                        return injected
                    t["done"] = True
                    return {"status": "completed", "text": text}
            candidates = [t["text"] for t in self.todos]
            return {"error": f"No todo with text {text!r}. Existing: {candidates}"}
        return {"error": f"Unknown tool: {name}"}

    def snapshot(self) -> Dict[str, Any]:
        return {"todos": [dict(t) for t in self.todos]}


class OfficeEnv:
    """文件 + 待办合体环境（vfs 工具 ∪ todo 工具）：真实办公任务同时落在两类状态上
    （读笔记 → 建待办 → 写剩余清单），单一环境才能对终态一并断言。

    委派给 VfsEnv / TodoEnv，自己不持状态；env_init 直接透传
    （files / todos / faults / readonly 全部有效）。快照合并两侧的键。"""

    def __init__(self, init: Dict[str, Any] = None):
        # 两侧各持自己的故障副本：faults 表按工具名分家（VfsEnv 只认文件工具名，
        # TodoEnv 只认待办工具名），同一配额不会被两侧各扣一次
        self.vfs = VfsEnv(init)
        self.todo = TodoEnv(init)
        self._vfs_names = {t["name"] for t in self.vfs.tools()}
        self._todo_names = {t["name"] for t in self.todo.tools()}

    def tools(self) -> List[Dict[str, Any]]:
        return self.vfs.tools() + self.todo.tools()

    def handle(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name in self._vfs_names:
            return self.vfs.handle(name, args)
        if name in self._todo_names:
            return self.todo.handle(name, args)
        return {"error": f"Unknown tool: {name}"}

    def snapshot(self) -> Dict[str, Any]:
        return {**self.vfs.snapshot(), **self.todo.snapshot()}


MOCK_ENVS = {"vfs": VfsEnv, "todo": TodoEnv, "office": OfficeEnv}


def make_env(name: str, init: Dict[str, Any] = None):
    if name not in MOCK_ENVS:
        raise ValueError(f"未知 mock 环境: {name!r}（可用: {sorted(MOCK_ENVS)}）")
    return MOCK_ENVS[name](init)


def env_response_content(resp: Dict[str, Any]) -> str:
    return json.dumps(resp, ensure_ascii=False)
