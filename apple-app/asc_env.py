"""ASC API 凭据寻址（`asc-update-signing.py` / `wait-testflight.py` 共用）。

**私钥 .p8 永远在仓库外**（`~/.appstoreconnect/private_keys/`）。这里只解析两个
标识符（Key ID / Issuer ID），它们从 `.env.local` 来——与仓库既有惯例同一份文件、
同一条规矩（`.env.example` 是模板，`.env.local` 不入库）。

标识符单独无法鉴权，参数化不是因为它们是密钥，而是**入了 git 历史就抹不掉**：
日后仓库若转公开，不必回头洗历史。

写成共用模块而不是两个脚本各抄一份——R1 的教训：拷贝一定会漂移。
"""
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_env_local() -> dict:
    """解析仓库根 `.env.local`（devctl.sh source 的同一份）。缺文件不算错——
    调用方可能已经把变量 export 好了。"""
    out = {}
    for name in (".env", ".env.local"):        # 与 devctl.sh 同序：.local 后覆盖
        f = _ROOT / name
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def asc_credentials() -> "tuple[str, str, Path]":
    """返回 (key_id, issuer, key_path)。缺值就**明确报错退出**，不静默跑错——
    半路才因 401 失败会让人以为是苹果的问题。"""
    env = {**_load_env_local(), **{k: v for k, v in os.environ.items() if v}}
    key_id = env.get("ASC_KEY_ID", "").strip()
    issuer = env.get("ASC_ISSUER_ID", "").strip()
    if not key_id or not issuer:
        raise SystemExit(
            "❌ 缺少 ASC_KEY_ID / ASC_ISSUER_ID。\n"
            f"   请在 {_ROOT / '.env.local'} 里填写（模板见 .env.example）：\n"
            "     ASC_KEY_ID=<App Store Connect 后台的 Key ID>\n"
            "     ASC_ISSUER_ID=<同页的 Issuer ID>\n"
            "   私钥 .p8 仍放 ~/.appstoreconnect/private_keys/，不要移进仓库。")
    key_path = Path.home() / f".appstoreconnect/private_keys/AuthKey_{key_id}.p8"
    if not key_path.exists():
        raise SystemExit(f"❌ 找不到私钥 {key_path}（ASC_KEY_ID={key_id}）。")
    return key_id, issuer, key_path
