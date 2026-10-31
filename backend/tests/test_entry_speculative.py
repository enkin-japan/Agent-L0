"""D.21 条目级投机覆盖：合并语义、装载期解析、两消费方接线（2026-08-17）。

守的不变量：
- merge_entry_speculative 是**纯字典合并**（不解析路径、不碰网络——首过评审
  C1/C2：解析在这里做会让 remote/edge 机白拽草稿权重、堵死事件循环）；
- 优先级 env 强制 > 条目 > 全局段；条目 mode 拼错响亮抛错（§S.3）；
- 路径解析下沉到 MLX 装载线程（_resolve_draft_path：本地目录放行、仓 id 按
  draft_revision 钉扎解析）；
- 真 config 钉：qwen38 两档配对与钉扎 SHA 恒等，其余条目无 speculative 键，
  全局段恒 off；
- 接线在位钉：server / engine.create / fetch_model 三处消费点存在（13:57 类
  事故住在接线处不在纯函数里——评审 M10；行为面由 D.21 实机冒烟背书）。
"""
from pathlib import Path

import pytest

import framework.utils.model_selection as ms
from framework.utils.model_selection import merge_entry_speculative

REPO_ROOT = Path(__file__).resolve().parents[2]

_ENTRY = {"repo": "x/y", "speculative": {
    "mode": "drafter", "draft_model": "x/y-MTP",
    "draft_revision": "abc123", "draft_block_size": 5}}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """本文件恒不许触资源解析：merge 是纯函数，任何解析调用都是回归（评审 M11）。"""
    import framework.utils.path_resolver as pr
    monkeypatch.setattr(pr, "resolve_model_path",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("merge 路径不该解析模型路径")))


def test_no_entry_spec_passthrough():
    cfg = {"speculative": {"mode": "off"}, "kv_persist": True}
    assert merge_entry_speculative(cfg, {"repo": "x/y"}) == cfg


def test_env_forced_suppresses_entry():
    cfg = {"speculative": {"mode": "off"}}
    out = merge_entry_speculative(cfg, _ENTRY, env_forced=True)
    assert out["speculative"] == {"mode": "off"}


def test_entry_overrides_globals_pure_merge():
    cfg = {"speculative": {"mode": "off", "num_draft_tokens": 8}}
    out = merge_entry_speculative(cfg, _ENTRY)
    spec = out["speculative"]
    assert spec["mode"] == "drafter"
    assert spec["draft_model"] == "x/y-MTP"       # 保持仓 id 原样：解析在装载线程
    assert spec["draft_revision"] == "abc123"     # 钉扎随 dict 传递到装载处
    assert spec["draft_block_size"] == 5
    assert spec["num_draft_tokens"] == 8          # 全局段未被条目覆盖的键保留
    assert cfg["speculative"]["mode"] == "off"    # 入参不被改动


def test_entry_bad_mode_raises_loudly():
    entry = {"repo": "x/y", "speculative": {"mode": "mtp"}}
    with pytest.raises(ValueError, match="off|prompt_lookup|drafter"):
        merge_entry_speculative({"speculative": {}}, entry)


# ---- 装载期解析（MLX 侧 _resolve_draft_path）----

def test_resolve_draft_local_dir_passthrough(tmp_path, monkeypatch):
    from framework.backends.mlx.backend import _resolve_draft_path
    import framework.utils.path_resolver as pr
    monkeypatch.setattr(pr, "resolve_model_path",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("本地目录不该解析")))
    assert _resolve_draft_path(str(tmp_path), {}) == str(tmp_path)


def test_resolve_draft_repo_id_pins_revision(monkeypatch):
    from framework.backends.mlx import backend as mb
    import framework.utils.path_resolver as pr
    seen = {}

    def fake(repo, revision=None):
        seen["repo"], seen["revision"] = repo, revision
        return "/models/y-MTP"

    monkeypatch.setattr(pr, "resolve_model_path", fake)
    out = mb._resolve_draft_path("x/y-MTP", {"draft_revision": "abc123"})
    assert out == "/models/y-MTP"
    assert seen == {"repo": "x/y-MTP", "revision": "abc123"}


# ---- 真 config 钉（D.21 阵容事实）----

def test_real_config_d21_speculative_pins():
    """qwen38 MLX 条目声明 drafter 配对（钉扎 SHA 恒等；D27 起只剩 4bit 一档，8bit 已出册）；
    其余条目一律无 speculative 键。"""
    reg = ms.load_section("inference").get("model_registry") or []
    by_repo = {e.get("repo"): e for e in reg}
    assert "mlx-community/Qwen3.8-27B-8bit" not in by_repo
    q4 = by_repo["mlx-community/Qwen3.8-27B-4bit"].get("speculative")
    assert q4 == {"mode": "drafter",
                  "draft_model": "mlx-community/Qwen3.8-27B-MTP-4bit",
                  "draft_revision": "b643c01b6d3b094e325edb6ebd832e16c486c575",
                  "draft_block_size": 3}
    others = [r for r, e in by_repo.items()
              if e.get("speculative") and not r.startswith("mlx-community/Qwen3.8")]
    assert not others, f"非 qwen38 条目不该声明 speculative（gemma 路线已判死）: {others}"
    # 全局段仍 off：条目级是唯一开启面，防全局误扳影响全部档
    assert str((ms.load_section("inference").get("speculative") or {}).get("mode")) == "off"


# ---- 接线在位钉（评审 M10；源文本存在性钉，行为面由 D.21 冒烟背书）----

def test_wiring_sites_present():
    server = (REPO_ROOT / "inference/server.py").read_text(encoding="utf-8")
    assert 'merge_entry_speculative(_inf_cfg, model_options(model),' in server
    assert 'env_forced=("mode" in _spec_env)' in server   # 只有 INFER_SPEC_MODE 压制条目
    engine = (REPO_ROOT / "backend/framework/core/engine.py").read_text(encoding="utf-8")
    assert "merge_entry_speculative(dict(kv_config or {})" in engine
    fetch = (REPO_ROOT / "backend/scripts/fetch_model.py").read_text(encoding="utf-8")
    assert '"drafter"' in fetch and "draft_revision" in fetch   # 前台预拉草稿件在位
