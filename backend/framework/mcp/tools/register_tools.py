"""register_api_tool / register_skill：对话式注册（D.18 完整版，DEFERRED §4）。

模型在对话中起草声明 → 本工具校验后落**待确认区**（pending_registrations），
不进生效目录、不进装配。用户在设置页审批队列里看全文 → 确认才真正注册。

两条红线的执行位置：
① 密钥不走对话通道——参数面**没有**密钥字段（模型物理上传不了实值）；声明里
  形似密钥实值的内容在提交口拒绝（find_inline_secret）。auth 只收环境变量名。
② 不可静默生效——本模块只会写 pending 文件；确认端点只在设置页 API 上，
  模型没有确认工具。requires_approval=True 是第一道闸（调用时用户看到声明全文），
  待确认队列是第二道（异步细看后放行），双闸有意冗余。
"""
from typing import Any, Dict, List, Optional

from ..base import BaseTool


def _factory():
    from . import api_tool_factory
    return api_tool_factory


class RegisterApiToolTool(BaseTool):
    name = "register_api_tool"
    description = (
        "Draft a new HTTP API tool and submit it for the user's confirmation. It does "
        "NOT take effect immediately: the declaration lands in a pending queue that the "
        "user reviews in Settings. NEVER put a real API key/token anywhere in the "
        "declaration or the conversation — set auth_env to an ENVIRONMENT VARIABLE NAME "
        "(e.g. WEATHER_API_KEY) and tell the user to enter the actual value in the "
        "Settings key field after confirming. Declarations containing literal secrets "
        "are rejected."
    )
    parameters = {
        "properties": {
            "name": {"type": "string",
                     "description": "Tool name, lowercase snake_case (e.g. weather_now)."},
            "description": {"type": "string",
                            "description": "What the tool does and when to use it — "
                                           "quality here decides how well the tool gets "
                                           "used. Write it for the model that will call it."},
            "endpoint": {"type": "string",
                         "description": "Full http(s) URL. Path placeholders like "
                                        "{city} are filled from arguments."},
            "method": {"type": "string",
                       "description": "HTTP method (GET/POST/PUT/PATCH/DELETE); "
                                      "default GET."},
            "params": {"type": "object",
                       "description": "JSON Schema fragment for arguments: "
                                      "{properties: {...}, required: [...]}."},
            "headers": {"type": "object",
                        "description": "Static extra headers. NO auth headers here — "
                                       "use auth_env/auth_header instead."},
            "timeout": {"type": "number", "description": "Seconds; default 20."},
            "auth_env": {"type": "string",
                         "description": "ENVIRONMENT VARIABLE NAME holding the key "
                                        "(never the key itself). Omit if no auth."},
            "auth_header": {"type": "string",
                            "description": "Header carrying the key (default "
                                           "Authorization)."},
            "auth_format": {"type": "string",
                            "description": "Header value template, {key} = secret "
                                           "(default 'Bearer {key}')."},
            "requires_approval": {"type": "boolean",
                                  "description": "Ask user before each call; defaults "
                                                 "true for mutating methods."},
            "note": {"type": "string",
                     "description": "One-line rationale shown to the user in the "
                                    "pending queue."},
        },
        "required": ["name", "description", "endpoint"],
    }
    requires_approval = True
    is_concurrency_safe = False

    def execute(self, name: str, description: str, endpoint: str,
                method: str = "GET", params: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None,
                auth_env: str = "", auth_header: str = "", auth_format: str = "",
                requires_approval: Optional[bool] = None,
                note: str = "") -> str:
        from ...core import pending_registrations as pending
        factory = _factory()
        decl: Dict[str, Any] = {"name": str(name).strip(),
                                "description": str(description).strip(),
                                "endpoint": str(endpoint).strip(),
                                "method": str(method or "GET").upper()}
        if params:
            decl["params"] = params
        if headers:
            decl["headers"] = headers
        if timeout is not None:
            decl["timeout"] = float(timeout)
        if str(auth_env).strip():
            auth: Dict[str, Any] = {"env": str(auth_env).strip()}
            if str(auth_header).strip():
                auth["header"] = str(auth_header).strip()
            if str(auth_format).strip():
                auth["format"] = str(auth_format).strip()
            decl["auth"] = auth
        if requires_approval is not None:
            decl["requires_approval"] = bool(requires_approval)

        problem = factory._validate(decl)
        if problem:
            return f"声明不合法，未提交：{problem}"
        leak = pending.find_inline_secret(decl)
        if leak:
            return f"已拒绝：{leak}"
        # 撞名检查：仓库手写声明 / 已生效用户声明 / 现役工具，都不许静默顶替
        taken = {item["decl"].get("name")
                 for item in (factory.load_declarations(factory.DECL_DIR, "repo")
                              + factory.load_declarations(factory.user_decl_dir(), "user"))
                 if isinstance(item["decl"], dict)}
        if decl["name"] in taken:
            return f"已拒绝：工具名 {decl['name']} 已存在（改名重提，或让用户在设置页改现有声明）"

        rid = pending.submit("api_tool", decl["name"], decl, note=str(note or ""))
        need_key = (f"该工具需要密钥：请用户在设置页密钥框为 {decl['auth']['env']} 录入实值"
                    f"（实值绝不要发到对话里）。" if decl.get("auth") else "")
        return (f"已提交待确认（注册单 {rid}）。声明不会立即生效——请用户到设置页"
                f"「待确认注册」里审阅并确认。{need_key}")


class RegisterSkillTool(BaseTool):
    name = "register_skill"
    description = (
        "Draft a new skill playbook (markdown workflow) and submit it for the user's "
        "confirmation. It does NOT take effect immediately: it lands in a pending queue "
        "the user reviews in Settings. Write the body as concrete numbered steps using "
        "existing tools; list those tools in requires."
    )
    parameters = {
        "properties": {
            "name": {"type": "string",
                     "description": "Skill name: lowercase letters/digits/hyphens "
                                    "(e.g. weekly-report)."},
            "description": {"type": "string",
                            "description": "One line: what task this playbook covers "
                                           "(shown in the skill index)."},
            "body": {"type": "string",
                     "description": "The playbook markdown: numbered steps, which tool "
                                    "at each step, what to verify before moving on."},
            "requires": {"type": "array", "items": {"type": "string"},
                         "description": "Tool names the playbook depends on."},
            "note": {"type": "string",
                     "description": "One-line rationale shown to the user in the "
                                    "pending queue."},
        },
        "required": ["name", "description", "body"],
    }
    requires_approval = True
    is_concurrency_safe = False

    def execute(self, name: str, description: str, body: str,
                requires: Optional[List[str]] = None, note: str = "") -> str:
        import re as _re
        from ...core import pending_registrations as pending
        from ...agents import runtime
        name = str(name).strip()
        if not _re.match(r"^[a-z0-9][a-z0-9-]*$", name):
            return "已拒绝：剧本名非法（小写字母/数字开头，其余含小写字母/数字/连字符）"
        if not str(description).strip() or not str(body).strip():
            return "已拒绝：description 与 body 均不可为空"
        reg = runtime.get_skill_registry()
        if reg is not None and reg.origin_of(name) == "repo":
            return f"已拒绝：与内置剧本 {name} 重名"
        payload = {"name": name, "description": str(description).strip(),
                   "body": str(body), "requires": [str(t) for t in (requires or [])]}
        leak = pending.find_inline_secret(payload)
        if leak:
            return f"已拒绝：{leak}"
        rid = pending.submit("skill", name, payload, note=str(note or ""))
        return (f"已提交待确认（注册单 {rid}）。剧本不会立即生效——请用户到设置页"
                f"「待确认注册」里审阅并确认。")
