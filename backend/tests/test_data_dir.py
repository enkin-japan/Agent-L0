"""可写数据根旋钮金测试（EXECUTION_LEDGER R5-4 / D.16）。

守的是**「代码在哪」与「数据写哪」的分家**：装进 .app 后代码树在签名的 bundle 里，
运行时写入必须全部挂 `AGENT_DATA_DIR`；dev 不设变量时一切落 `backend/`，与分家前
逐字节一致。

这类退化同样安静（与 test_edge_slimming 同病）：开发机上 `backend/` 永远可写，
谁把一个新写点锚回 `BASE_DIR`，本机毫无症状——**只有装进 bundle 首次运行才炸**
（写签名 bundle = SIGKILL 或签名失效），而那时离引入已经很远。
"""
import ast
import importlib
import pathlib

from framework.utils import config_loader
from framework.utils.config_loader import data_dir
from framework.utils.path_resolver import _models_root

_BACKEND = pathlib.Path(__file__).resolve().parent.parent


# ── 旋钮本体 ──────────────────────────────────────────────────────────

def test_default_is_backend_dir(monkeypatch):
    """不设变量 = dev 手感：数据根就是 backend/，与分家前完全一致。"""
    monkeypatch.delenv("AGENT_DATA_DIR", raising=False)
    assert data_dir() == config_loader.BACKEND_DIR


def test_env_redirects(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    assert data_dir() == tmp_path


def test_env_expands_user(monkeypatch):
    """app 注入的是 ~/Library/Application Support/… 一类路径，~ 必须展开。"""
    monkeypatch.setenv("AGENT_DATA_DIR", "~/agent-data-dir-test")
    assert data_dir() == pathlib.Path.home() / "agent-data-dir-test"


def test_blank_env_falls_back(monkeypatch):
    """空串/纯空白 = 未设置：launchd 的 EnvironmentVariables 留空值是常见事故。"""
    monkeypatch.setenv("AGENT_DATA_DIR", "  ")
    assert data_dir() == config_loader.BACKEND_DIR


# ── 三个低层写点跟随旋钮 ──────────────────────────────────────────────

def test_models_root_follows_knob(monkeypatch, tmp_path):
    """权重下载是运行时写：app 模式下必须落数据根，不能落 bundle。"""
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    assert _models_root() == tmp_path / "models"
    monkeypatch.delenv("AGENT_DATA_DIR")
    assert _models_root() == config_loader.BACKEND_DIR / "models"


def test_guardrail_events_follow_knob(monkeypatch, tmp_path):
    """events._PATH 在 import 期定格——用 reload 模拟「app 注入 env 后启动」。"""
    from framework.core import events
    old = events.get_path()
    try:
        monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
        importlib.reload(events)
        assert events.get_path() == tmp_path / "guardrail_events.jsonl"
    finally:
        monkeypatch.delenv("AGENT_DATA_DIR", raising=False)
        importlib.reload(events)
        events.set_path(old)   # 还原，绝不让本测试改变其他测试看到的落盘位置


# ── 写点清单守卫：新写点不得锚回代码树 ────────────────────────────────

def test_app_data_dirs_not_anchored_on_base_dir():
    """AST 扫 app.py 顶层赋值：数据目录必须派生自 DATA_DIR，不是 BASE_DIR。

    BASE_DIR 从分家起只许锚**只读内容**（TOOLS_DIR / SKILLS_DIR / 打印包）。
    谁把 SESSIONS_DIR 一类写点改回 `BASE_DIR / ...`，这里立刻红——不必等到装包。
    """
    tree = ast.parse((_BACKEND / "app.py").read_text(encoding="utf-8"))
    anchors = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        t = node.targets[0]
        if not isinstance(t, ast.Name):
            continue
        # 形如 X = <Name> / "literal" 的赋值，记根锚点
        v = node.value
        while isinstance(v, ast.BinOp):
            v = v.left
        if isinstance(v, ast.Name):
            anchors[t.id] = v.id

    for var in ("SESSIONS_DIR", "SCHEDULES_DIR", "NOTES_DIR"):
        assert anchors.get(var) == "DATA_DIR", \
            f"{var} 锚在 {anchors.get(var)!r}，写点必须挂 DATA_DIR（D.16）"
    for var in ("TOOLS_DIR",):
        assert anchors.get(var) == "BASE_DIR", \
            f"{var} 是只读内容，应锚 BASE_DIR（代码树）"


def test_push_registry_not_anchored_on_base_dir():
    """push/ 是第 4 个写点，藏在函数体里（AST 顶层扫不到）——文本断言兜住。"""
    src = (_BACKEND / "app.py").read_text(encoding="utf-8")
    assert 'PushRegistry(DATA_DIR / "push")' in src
    assert 'PushRegistry(BASE_DIR' not in src


def test_apns_key_falls_back_to_data_dir():
    """APNs 密钥相对路径：仓库根落空 → 数据根兜底（R5-9）。bundle 形态仓库根 =
    只读 Resources/，私钥永不入包，数据根（Application Support）是它唯一的家。"""
    src = (_BACKEND / "app.py").read_text(encoding="utf-8")
    assert "primary if primary.exists() else DATA_DIR / key_path" in src
