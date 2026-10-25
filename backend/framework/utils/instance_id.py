"""本机后端在 iPhone 眼里是哪台 Mac：Mac app 生成的稳定标识（即 iCloud 同步条目里的
macId），由内建执行层的启动环境 AGENT_INSTANCE_ID 注入。

iPhone 可能保存了多台 Mac：推送的 `ma.src`、Live Activity 的 `src` 与配对 claim 的
`instance_id` 让它知道消息来自哪台、审批该回给哪台。这不是密钥，只是一块名字牌。
未注入（devctl 直起）或格式不对时为 None：各处照旧不带该字段，iPhone 退回「当前那台」。
"""
import os
import re

_VALID = re.compile(r"[A-Za-z0-9-]{8,64}")


def instance_id():
    value = os.environ.get("AGENT_INSTANCE_ID", "").strip()
    return value if _VALID.fullmatch(value) else None
