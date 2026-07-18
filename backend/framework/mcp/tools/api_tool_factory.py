"""api_tool_factory：声明式私人 API 工具工厂（用户拍板「私人 API 变工具流程自动化」）。

`api_tools/*.yaml`（仓库根，与 skills/ 同级——声明是内容不是代码）一份声明 = 一个工具。
工厂在模块导入时读声明、动态合成 BaseTool 子类挂进本模块命名空间：目录扫描用
inspect.getmembers 找类并**零参实例化**，动态类天然被扫到——registry 零改动，
「即插即用」纪律的延伸：新私人 API = 写一个 yaml + rescan/重启。

声明格式（name/description/endpoint 必填）：
    name: weather_home          # 工具名：小写蛇形，全局唯一（与现有工具撞名=未定义行为，自己保证）
    description: >              # 给模型的使用说明（何时调、返回什么）——一等公民，强制非空
      ...
    endpoint: "https://api.example.com/v1/thing/{id}"   # {param} 路径模板，实参 URL 编码替换
    method: GET                 # GET/HEAD/POST/PUT/PATCH/DELETE，默认 GET
    params:                     # JSON Schema 片段（与 BaseTool.parameters 同形），可省
      properties:
        id: {type: string, description: which thing}
      required: [id]
    auth:                       # 可选。密钥红线：这里只写环境变量名，实值在 .env.local（不入 git）
      env: MY_API_KEY
      header: Authorization     # 默认 Authorization
      format: "Bearer {key}"    # 默认 "Bearer {key}"
    headers: {X-Static: v}      # 可选静态头
    timeout: 30                 # HTTP 超时秒，默认 30（编排层等待自动加余量）
    requires_approval: false    # 缺省按方法：GET/HEAD 免审批，变更方法弹审批；显式声明可覆盖
    enabled: true               # false = 停用不删除（工具数纪律：最小相关集，schema 都进稳定前缀）
    canary:                     # 可选：漂移巡检探针（私人 API 属 B 类 api drift）
      arguments: {id: "1"}      # 用这组实参真实调一次，HTTP >= 400 即失败

安全边界：
- 密钥永不进声明文件；auth.env 变量缺失 → is_available False 不注册（探活纪律：
  不给模型必然失败的工具），.env.local 补上后 rescan 即上架。
- 审批默认保守：变更类方法默认 requires_approval=True；声明是用户自己写的授权面，
  显式值优先。
- 参数注入位置固定：路径模板 {param} 用掉的实参不再重复发送；其余 GET/HEAD 进
  query string，变更方法进 JSON body。响应原文返回（观察截断由压缩层统一处理）。
- 坏声明 fail-open：解析失败/校验不过只废那一份并打印原因，不影响其他工具加载。

canary 只在声明了探针时才挂到合成类上——巡检的纳入判据是「类覆写了 canary」，
无差别覆写会把没探针的 API 工具也拖进巡检并按形状非法记失败（假告警）。
"""
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ..base import BaseTool

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

#: 手写声明目录：仓库根 api_tools/（backend 的兄弟目录，dev 与 app bundle 同布局——
#: 与 app.py 的 SKILLS_DIR = BASE_DIR.parent/"skills" 同一解析逻辑）
DECL_DIR = Path(__file__).resolve().parents[4] / "api_tools"


def user_decl_dir() -> Path:
    """UI 添加的声明目录：数据根下 api_tools/。**不能写仓库目录**——app 模式下
    代码树在签名的 bundle 里，写进去破坏签名（guardrail_events 同一教训）。
    每次现读 data_dir()（进程内 env 不变，测试要能各自注入）。"""
    from ...utils.config_loader import data_dir
    return data_dir() / "api_tools"


def secrets_path() -> Path:
    """UI 录入的密钥文件（0600，数据根下，天然不入 git）。格式：每行 KEY=VALUE。"""
    return user_decl_dir() / ".secrets.env"


def read_secret(env_name: str) -> str:
    """密钥读取顺序：环境变量（.env.local，devctl source）优先 → secrets 文件兜底。"""
    val = os.environ.get(env_name, "")
    if val:
        return val
    try:
        for line in secrets_path().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == env_name:
                    return v.strip()
    except OSError:
        pass
    return ""


def write_secret(env_name: str, value: str) -> None:
    """upsert 一条密钥（建目录、去重旧行、写回后 chmod 0600）。"""
    path = secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    try:
        lines = [l for l in path.read_text(encoding="utf-8").splitlines()
                 if l.strip() and l.split("=", 1)[0].strip() != env_name]
    except OSError:
        pass
    lines.append(f"{env_name}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _validate(decl: Any) -> Optional[str]:
    """返回问题描述；None = 合法。"""
    if not isinstance(decl, dict):
        return "声明不是映射"
    name = decl.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return f"name 非法：{name!r}（小写字母开头的蛇形）"
    if not str(decl.get("description") or "").strip():
        return "description 为空——工具好不好用九成在这句话，必填"
    if not str(decl.get("endpoint") or "").startswith(("http://", "https://")):
        return "endpoint 必须是 http(s) URL"
    method = str(decl.get("method") or "GET").upper()
    if method not in _METHODS:
        return f"method 非法：{method}"
    auth = decl.get("auth")
    if auth is not None and not (isinstance(auth, dict) and auth.get("env")):
        return "auth 缺 env（只写环境变量名，实值在 .env.local）"
    params = decl.get("params") or {}
    if not isinstance(params, dict) or not isinstance(params.get("properties") or {}, dict):
        return "params 非法（需为含 properties 映射的 JSON Schema 片段）"
    canary = decl.get("canary")
    if canary is not None and not isinstance(canary, dict):
        return "canary 非法（需为含 arguments 的映射）"
    return None


class _ApiCore:
    """声明驱动 HTTP 工具的共同实现。

    刻意**不是** BaseTool 子类：目录扫描会把模块里每个 BaseTool 子类零参实例化，
    本类构造需要声明入参，混进扫描会炸掉整个模块的加载。合成类以
    (_ApiCore, BaseTool) 双基类拼出完整工具。
    """

    #: 试调通道（/api-tools/test）：未保存的密钥直接挂实例，绕过 env/secrets 读取
    _secret_override: Optional[str] = None

    def _bind(self, decl: Dict[str, Any]) -> None:
        self._decl = decl
        self.name = decl["name"]
        self.description = str(decl["description"]).strip()
        params = decl.get("params") or {}
        self.parameters = {
            "properties": dict(params.get("properties") or {}),
            "required": list(params.get("required") or []),
        }
        self._method = str(decl.get("method") or "GET").upper()
        self._endpoint = str(decl["endpoint"])
        self._timeout = float(decl.get("timeout") or 30)
        # 编排层等待 = HTTP 超时 + 余量（长超时声明不被全局默认掐断）
        self.timeout_seconds = int(self._timeout) + 30
        ra = decl.get("requires_approval")
        self.requires_approval = bool(ra) if ra is not None else self._method in _MUTATING
        self.is_concurrency_safe = self._method in ("GET", "HEAD")
        self.is_read_only = self._method in ("GET", "HEAD")
        self.drift_class = "api"
        self.drift_fix = (f"私人 API 探针连续失败：检查服务是否在跑、"
                          f"api_tools/ 里 {self.name} 的声明是否仍与服务端一致")

    def _key_value(self) -> str:
        if self._secret_override is not None:
            return self._secret_override
        auth = self._decl.get("auth")
        return read_secret(str(auth["env"])) if auth else ""

    def is_available(self) -> bool:
        auth = self._decl.get("auth")
        if auth and not self._key_value():
            print(f"[ApiTool] ⛔ {self.name}：密钥 {auth['env']} 未设置"
                  f"（.env.local 或 UI 录入），不注册。")
            return False
        return True

    def _request(self, arguments: Dict[str, Any]) -> Tuple[int, str]:
        import requests
        url = self._endpoint
        args = dict(arguments or {})
        for key in re.findall(r"\{([A-Za-z0-9_]+)\}", self._endpoint):
            if key in args:
                url = url.replace("{" + key + "}",
                                  urllib.parse.quote(str(args.pop(key)), safe=""))
        headers = {str(k): str(v) for k, v in (self._decl.get("headers") or {}).items()}
        auth = self._decl.get("auth")
        if auth:
            headers[str(auth.get("header") or "Authorization")] = str(
                auth.get("format") or "Bearer {key}").replace("{key}", self._key_value())
        kw: Dict[str, Any] = {}
        if self._method in ("GET", "HEAD"):
            if args:
                kw["params"] = args
        elif args:
            kw["json"] = args
        resp = requests.request(self._method, url, headers=headers,
                                timeout=self._timeout, **kw)
        return resp.status_code, resp.text

    def execute(self, **kwargs) -> str:
        try:
            status, text = self._request(kwargs)
        except Exception as e:
            return f"API 调用失败（{self.name}）：{e}"
        body = (text or "").strip()
        if status >= 400:
            return f"API 返回错误 HTTP {status}（{self.name}）：{body[:500]}"
        return body or f"（HTTP {status}，空响应体）"


def _canary_method(self):
    """声明了 canary 的合成类才挂本方法（巡检纳入判据=类覆写了 canary）。"""
    args = (self._decl.get("canary") or {}).get("arguments") or {}
    status, text = self._request(args)
    ok = status < 400
    return ok, f"HTTP {status}" + ("" if ok else f"：{(text or '')[:200]}")


def synth_class(decl: Dict[str, Any], origin: str = "?") -> type:
    """把一份**已通过 _validate** 的声明合成为零参可实例化的工具类
    （registry 扫描/load_tool 契约）。canary 只在声明了探针时才挂（巡检纳入判据）。"""

    def __init__(self):
        self._bind(decl)

    ns: Dict[str, Any] = {
        "__init__": __init__,
        "__doc__": f"声明式 API 工具（{origin}）",
    }
    if decl.get("canary") is not None:
        ns["canary"] = _canary_method
    return type(f"ApiTool_{decl['name']}", (_ApiCore, BaseTool), ns)


def load_declarations(decl_dir: Path, source: str) -> List[Dict[str, Any]]:
    """读目录下全部 *.yaml/*.yml 声明（含坏的），供装配与管理端点共用。
    每项：{file, source, decl, problem}；problem 非 None = 坏声明（fail-open）。"""
    out: List[Dict[str, Any]] = []
    if not decl_dir.exists():
        return out
    for f in sorted(decl_dir.glob("*.yaml")) + sorted(decl_dir.glob("*.yml")):
        try:
            decl = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception as e:
            out.append({"file": f.name, "source": source, "decl": None,
                        "problem": f"解析失败：{e}"})
            continue
        out.append({"file": f.name, "source": source, "decl": decl,
                    "problem": _validate(decl)})
    return out


def build_tool_classes(decl_dir: Path, seen: Optional[set] = None,
                       source: str = "api_tools") -> List[type]:
    """读目录声明合成工具类列表。坏声明/停用/重名 fail-open 跳过并打印原因。
    seen 跨目录去重（仓库目录先扫、先到先得）。纯函数：金测试直接喂临时目录。"""
    classes: List[type] = []
    seen = seen if seen is not None else set()
    for item in load_declarations(decl_dir, source):
        decl, problem = item["decl"], item["problem"]
        if problem:
            print(f"[ApiTool] ⚠️ 声明 {item['file']} 非法，跳过：{problem}")
            continue
        if decl.get("enabled", True) is False:
            continue
        if decl["name"] in seen:
            print(f"[ApiTool] ⚠️ 声明 {item['file']} 工具名重复：{decl['name']}，跳过后者。")
            continue
        seen.add(decl["name"])
        classes.append(synth_class(decl, f"{source}/{item['file']}"))
    return classes


def _install() -> None:
    """模块导入即装配：合成类挂进模块命名空间，交给 registry 的目录扫描发现。
    双目录：仓库 api_tools/（手写）先扫，数据根 api_tools/（UI 添加）后扫，
    跨目录同名先到先得。收进函数作用域——模块级循环变量会残留成模块属性，
    getmembers 把同一个类数两次 → registry 重复注册（实测冒烟抓到的坑）。"""
    seen: set = set()
    for cls in (build_tool_classes(DECL_DIR, seen, "api_tools")
                + build_tool_classes(user_decl_dir(), seen, "data/api_tools")):
        globals()[cls.__name__] = cls


_install()
