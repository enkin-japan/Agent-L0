"""模型契约静态校验：不下载权重，判定 HF 上的 checkpoint 能否被本机 mlx-vlm 加载。

动机：mlx-community 的 checkpoint 布局与转换器版本耦合（2026-07-06 re-upload 裁掉共享 KV
权重后，mlx-vlm 0.5.0 加载直接 Missing 54 parameters）。大尺寸模型开发机跑不动，但
「加载崩不崩」不需要实机——checkpoint 的参数名集合（index.json / safetensors header）
与本机 mlx-vlm 按 config.json 建出的模型骨架比对即可，权重数值无关紧要。

做法（镜像 mlx_vlm.utils.load_model 的构建路径，惰性、零下载）：
  1. 拉 config.json + model.safetensors.index.json（各几 KB~几百 KB）；
  2. 无 index 时用 HTTP Range 只取 safetensors 文件头拿张量名；
  3. get_model_and_args → ModelConfig.from_dict → Model(config) → nn.quantize
     （class_predicate 只看参数名，用名字集合冒充 weights 即可，与真实加载同构）；
  4. 双向差集 = load_weights(strict=True) 会报的 Missing / extra。
附带：tokenizer/模板文件 sha256 与基准（当前在用模型）比对——同家族应完全一致，
不一致说明 prompt 组装/解析层有漂移风险（不判失败，只警告）。

用法：
  .venv/bin/python scripts/check_model_contract.py                # 校验 config.yaml 注册表全部条目
  .venv/bin/python scripts/check_model_contract.py REPO REVISION  # 校验单个候选（钉版本前的甄别）
"""
import json
import struct
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HF = "https://huggingface.co"
TIMEOUT = 30
# 模板/分词层基准文件：与基准模型不一致即警告（prompt 组装与解析器按这套方言写死）
_TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json",
                    "chat_template.jinja", "processor_config.json")


def _get(url: str, **kw) -> requests.Response:
    r = requests.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def fetch_checkpoint_names(repo: str, revision: str) -> tuple:
    """返回 (参数名集合, 是否 mlx 格式)。优先 index.json；缺失则 Range 读 header。"""
    base = f"{HF}/{repo}/resolve/{revision}"
    try:
        index = _get(f"{base}/model.safetensors.index.json").json()
        names = set(index["weight_map"].keys())
        shards = sorted(set(index["weight_map"].values()))
    except requests.HTTPError:
        names, shards = set(), ["model.safetensors"]

    # safetensors 头：前 8 字节小端 u64 = 头长度，随后是含 __metadata__ 与全部张量名的 JSON。
    # 只 Range 这一段（几百 KB 级），拿格式标记；index 缺失时顺带拿张量名。
    r = _get(f"{base}/{shards[0]}", headers={"Range": "bytes=0-7"})
    header_len = struct.unpack("<Q", r.content)[0]
    if header_len > 50 * 1024 * 1024:
        raise ValueError(f"safetensors header 异常（{header_len} bytes），文件可能损坏")
    r = _get(f"{base}/{shards[0]}", headers={"Range": f"bytes=8-{7 + header_len}"})
    header = json.loads(r.content)
    is_mlx = (header.get("__metadata__") or {}).get("format") == "mlx"
    if not names:
        for shard in shards:
            if shard != shards[0]:
                r0 = _get(f"{base}/{shard}", headers={"Range": "bytes=0-7"})
                n = struct.unpack("<Q", r0.content)[0]
                r1 = _get(f"{base}/{shard}", headers={"Range": f"bytes=8-{7 + n}"})
                header.update(json.loads(r1.content))
        names = {k for k in header if k != "__metadata__"}
    return names, is_mlx


def build_expected_names(config: dict, checkpoint_names: set) -> set:
    """镜像 mlx_vlm.utils.load_model 的骨架构建（惰性数组，不 eval，31B 也只占元数据内存）。"""
    import mlx.nn as nn
    from mlx.utils import tree_flatten
    from mlx_vlm.utils import get_model_and_args, update_module_configs, skip_multimodal_module

    config = dict(config)
    model_class, _ = get_model_and_args(config=config)
    config.setdefault("text_config", config.pop("llm_config", {}))
    config.setdefault("vision_config", {})
    config.setdefault("audio_config", {})
    model_config = model_class.ModelConfig.from_dict(config)
    model_config = update_module_configs(model_config, model_class, config,
                                         ["text", "vision", "perceiver", "projector", "audio"])
    model = model_class.Model(model_config)

    if (quantization := config.get("quantization", None)) is not None:
        skip_vision = config.get("vision_config", {}).get("skip_vision", False)

        def get_class_predicate(p, m):  # 与 load_model 逐行同构：谓词只依赖参数名与模块形状
            if skip_multimodal_module(p) and skip_vision:
                return False
            if p in config["quantization"]:
                return config["quantization"][p]
            if not hasattr(m, "to_quantized"):
                return False
            if hasattr(m, "weight") and m.weight.size % 64 != 0:
                return False
            return f"{p}.scales" in checkpoint_names

        nn.quantize(model, group_size=quantization["group_size"], bits=quantization["bits"],
                    mode=quantization.get("mode", "affine"), class_predicate=get_class_predicate)

    return {name for name, _ in tree_flatten(model.parameters())}


def file_hashes(repo: str, revision: str) -> dict:
    """模板/分词层文件指纹：LFS 文件用 sha256，小文件退回 size+oid。"""
    info = _get(f"{HF}/api/models/{repo}/revision/{revision}?blobs=true").json()
    out = {}
    for s in info.get("siblings", []):
        name = s.get("rfilename")
        if name in _TOKENIZER_FILES:
            lfs = s.get("lfs") or {}
            out[name] = lfs.get("sha256") or f"blob:{s.get('blobId')}:{s.get('size')}"
    return out


def check_one(repo: str, revision: str, baseline_hashes: dict = None) -> dict:
    config = _get(f"{HF}/{repo}/resolve/{revision}/config.json").json()
    names, is_mlx = fetch_checkpoint_names(repo, revision)
    if not is_mlx:
        # 非 mlx 格式会走 sanitize 重命名路径，静态比对不再同构 → 明确拒判而不是误报
        return {"repo": repo, "revision": revision, "ok": False,
                "error": "checkpoint 非 mlx 格式，静态校验不适用（sanitize 路径无法离线复刻）"}
    expected = build_expected_names(config, names)
    missing = sorted(expected - names)
    extra = sorted(names - expected)
    result = {"repo": repo, "revision": revision, "ok": not missing and not extra,
              "n_params": len(expected), "missing": missing, "extra": extra}
    if baseline_hashes:
        drift = [f for f, h in file_hashes(repo, revision).items()
                 if baseline_hashes.get(f) and h != baseline_hashes[f]]
        result["template_drift"] = drift  # 非致命：提示 prompt/解析层需人工过目
    return result


def main() -> int:
    if len(sys.argv) == 3:
        candidates = [{"repo": sys.argv[1], "revision": sys.argv[2]}]
        baseline = None
    else:
        from framework.utils.config_loader import load_section
        inference = load_section("inference")
        candidates = inference.get("model_registry") or []
        if not candidates:
            print("config.yaml 的 inference.model_registry 为空，无可校验条目")
            return 1
        # 基准 = 注册表第一项（当前实机验证过的模型），模板漂移相对它报告
        baseline = file_hashes(candidates[0]["repo"], candidates[0]["revision"])

    failures = 0
    for c in candidates:
        try:
            r = check_one(c["repo"], c["revision"], baseline)
        except Exception as e:
            r = {"repo": c["repo"], "revision": c["revision"], "ok": False,
                 "error": f"{type(e).__name__}: {e}"}
        mark = "✅" if r["ok"] else "❌"
        line = f"{mark} {r['repo']} @ {r['revision'][:8]}"
        if r.get("error"):
            line += f" — {r['error']}"
        elif not r["ok"]:
            line += (f" — missing {len(r['missing'])} / extra {len(r['extra'])}"
                     f"（如 {(r['missing'] or r['extra'])[0]}）")
        else:
            line += f" — {r['n_params']} 参数名全部对齐"
            if r.get("template_drift"):
                line += f"；⚠️ 模板漂移: {', '.join(r['template_drift'])}"
        print(line)
        failures += 0 if r["ok"] else 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
