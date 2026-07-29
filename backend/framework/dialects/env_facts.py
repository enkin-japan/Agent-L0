"""环境事实行（方言无关，qwen35 方言波从 gemma4/prompt_builder 原样提取）。

「用户在什么机器/时区/语言环境」是**部署事实**，不是模型方言事实——任何方言的
prompt 里这些行的内容都一样，只有包裹它们的模板标记不同。收编在此供各方言 builder
共用，避免每加一个方言就复制一份（复制=漂移：改探测逻辑漏改一份，两方言各说各话）。

隐私边界不变：这些行只在主编排轮注入（builder 的 include_env 门控），个人事实
默认全空、opt-in（config.yaml agent.environment）。
"""
import os
import platform
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional


def iana_timezone() -> str:
    """IANA 时区名（如 Asia/Tokyo）：读 /etc/localtime 符号链接（macOS/Linux 通行）。
    读不到返回空串，由调用方退时区缩写。"""
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return ""


def system_locale() -> str:
    """系统区域标识（如 ja_JP / zh_CN）。macOS 下后端进程（launchd/app 拉起）常无 LANG
    环境变量，优先问系统偏好；失败退 LANG。启动时算一次，不在热路径。"""
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(["defaults", "read", "-g", "AppleLocale"],
                                 capture_output=True, text=True, timeout=3)
            val = out.stdout.strip()
            if val:
                return val
        except Exception:
            pass
    return os.environ.get("LANG", "").split(".")[0]


def compute_static_env_lines(env: Optional[Dict[str, Any]]) -> List[str]:
    """稳定环境事实（启动算一次）：机器事实自动探测 + config 里填写的个人事实。
    最小相关集，不是"足够多"——弱模型上下文越多注意力越稀释。个人事实默认全空，
    仅填写后注入；且只进主编排轮（builder include_env=True），不进沙箱子生成。"""
    env = env or {}
    lines: List[str] = []
    if env.get("inject_machine", True):
        sysname = platform.system()
        if sysname == "Darwin":
            ver = platform.mac_ver()[0]
            os_str = f"macOS {ver}" if ver else "macOS"
        elif sysname:
            os_str = f"{sysname} {platform.release()}".strip()
        else:
            os_str = "unknown OS"
        lines.append(f"Operating system: {os_str} ({platform.machine() or 'unknown arch'})")
    if env.get("inject_timezone", True):
        # 时区 = 免权限的稳定事实，一石二鸟：相对时间锚点（"明天/晚上"换算）+
        # 国家级粗位置（天气/本地检索兜底）。精确位置另有 get_location 工具按需查
        # （半易变事实不进静态块）。启动时算一次——时区变动极低频，重启即刷新。
        now = datetime.now().astimezone()
        off = now.strftime("%z")
        off_str = f"UTC{off[:3]}:{off[3:]}" if len(off) == 5 else ""
        name = iana_timezone() or now.strftime("%Z")
        if name or off_str:
            detail = " ".join(x for x in (off_str, now.strftime("%Z")) if x)
            lines.append(f"Time zone: {name}" + (f" ({detail})" if detail else ""))
    if env.get("language"):
        lines.append(f"User's preferred reply language: {env['language']}")
    elif env.get("inject_locale", True):
        # language 未填时的自动兜底：系统区域标识（数字/日期格式、粗粒度语言偏好线索）。
        # 显式填了 language 就不再注入——两行说同一件事只稀释注意力。
        loc = system_locale()
        if loc:
            lines.append(f"User's system locale: {loc}")
    if env.get("location"):
        lines.append(f"User's approximate location: {env['location']}")
    if env.get("about"):
        lines.append(f"About the user: {env['about']}")
    return lines
