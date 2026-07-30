"""长链维度的确定性 mock 工具环境（tau-bench 思路：可校验终态 + 脚本化世界）。

评的是多步工具链推理：读→算→写、错误恢复、不多做不漏做。环境是纯内存状态机，
handler 全部确定性——同一模型行为必得同一终态，可复现可回归。错误路径给真实
风格的错误回执（找不到文件等），模型的错误恢复行为也是测量面。

环境注册：MOCK_ENVS = {"vfs": 虚拟文件系统, "todo": 待办清单}。
题面用法：task.env = "vfs"，task.env_init = 初始状态；终态断言见 scorers/longchain。
"""
import json
from typing import Any, Dict, List


class VfsEnv:
    """虚拟文件系统：list_files / read_file / write_file / append_file / delete_file。"""

    def __init__(self, init: Dict[str, Any] = None):
        self.files: Dict[str, str] = dict((init or {}).get("files") or {})

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
        if name == "list_files":
            return {"files": sorted(self.files)}
        if name == "read_file":
            if path not in self.files:
                return {"error": f"File not found: {path}"}
            return {"path": path, "content": self.files[path]}
        if name == "write_file":
            if "content" not in args:
                return {"error": "Missing required parameter: content"}
            self.files[path] = str(args["content"])
            return {"status": "written", "path": path}
        if name == "append_file":
            if path not in self.files:
                return {"error": f"File not found: {path} (use write_file to create)"}
            self.files[path] += str(args.get("content", ""))
            return {"status": "appended", "path": path}
        if name == "delete_file":
            if path not in self.files:
                return {"error": f"File not found: {path}"}
            del self.files[path]
            return {"status": "deleted", "path": path}
        return {"error": f"Unknown tool: {name}"}

    def snapshot(self) -> Dict[str, Any]:
        return {"files": dict(self.files)}


class TodoEnv:
    """待办清单：todo_add / todo_list / todo_complete（按文本匹配，避免索引脆弱）。"""

    def __init__(self, init: Dict[str, Any] = None):
        self.todos: List[Dict[str, Any]] = [dict(t) for t in (init or {}).get("todos") or []]

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
            self.todos.append({"text": text, "done": False})
            return {"status": "added", "text": text}
        if name == "todo_list":
            return {"todos": [dict(t) for t in self.todos]}
        if name == "todo_complete":
            for t in self.todos:
                if t["text"] == text:
                    t["done"] = True
                    return {"status": "completed", "text": text}
            candidates = [t["text"] for t in self.todos]
            return {"error": f"No todo with text {text!r}. Existing: {candidates}"}
        return {"error": f"Unknown tool: {name}"}

    def snapshot(self) -> Dict[str, Any]:
        return {"todos": [dict(t) for t in self.todos]}


MOCK_ENVS = {"vfs": VfsEnv, "todo": TodoEnv}


def make_env(name: str, init: Dict[str, Any] = None):
    if name not in MOCK_ENVS:
        raise ValueError(f"未知 mock 环境: {name!r}（可用: {sorted(MOCK_ENVS)}）")
    return MOCK_ENVS[name](init)


def env_response_content(resp: Dict[str, Any]) -> str:
    return json.dumps(resp, ensure_ascii=False)
