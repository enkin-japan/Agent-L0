"""B4 判定：vLLM 与 mlx_vlm 在同模型同 γ 同 prompt 下的逐位接受率是否一致。

输入：
  · vLLM 侧 /content/cells/e4b_g*.json（已 colab download 到本地目录）
  · mlx 侧 scan_mtp_block.py 的日志（解析 block/加速比/P(≥k) 行）

判据：acceptance_rates[k-1] ≡ P(accepted ≥ k)，两边定义已逐条对过源码（见 vllm_cell.py 头注）。
若逐位分布在同 γ 下落在彼此的采样噪声内 ⇒ **两边跑的是同一个算法**，
差距全在「AL → 墙钟」的转化效率上，而非草稿质量。
"""
import glob
import json
import re
import sys

VLLM_DIR = sys.argv[1]
MLX_LOG = sys.argv[2]


def load_vllm():
    out = {}
    for p in glob.glob(f"{VLLM_DIR}/e4b_g*.json"):
        d = json.load(open(p))
        out[d["gamma"]] = d
    return out


def load_mlx(path):
    """解析形如：block  5 (γ=4)  解码tps ...  加速  2.09×  接受率 48.2% ...
                 P(≥k) k=1..4: 73.0% 50.4% ..."""
    cells, base = {}, None
    cur = None
    for line in open(path, encoding="utf-8", errors="replace"):
        # 两种日志格式都要吃：旧版 "baseline-前  tps 72.80"、新版 "baseline-前  解码tps 72.80"
        m = re.search(r"baseline-前\s+(?:解码)?tps\s+([\d.]+)", line)
        if m and base is None:
            base = float(m.group(1))
        m = re.search(r"block\s+(\d+)\s+\(γ=(\d+)\).*?加速\s+([\d.]+)×.*?接受率\s+([\d.]+)%", line)
        if m:
            cur = int(m.group(2))
            cells[cur] = {"gamma": cur, "speedup": float(m.group(3)),
                          "rate": float(m.group(4)) / 100}
            m2 = re.search(r"解码tps\s+([\d.]+)", line)
            if m2:
                cells[cur]["tps"] = float(m2.group(1))
            else:
                m3 = re.search(r"tps\s+([\d.]+)", line)
                cells[cur]["tps"] = float(m3.group(1)) if m3 else None
            continue
        m = re.search(r"P\(≥k\).*?:\s*(.+)$", line)
        if m and cur is not None:
            cells[cur]["P_ge_k"] = [float(x.rstrip("%")) / 100
                                    for x in m.group(1).split()]
    return base, cells


def main():
    v = load_vllm()
    base_mlx, m = load_mlx(MLX_LOG)
    base_v = v.get(0, {}).get("tps")

    print(f"基线：vLLM(A100, E4B bf16) {base_v:.2f} tps   "
          f"mlx(M4 Max, e4b 8bit) {base_mlx:.2f} tps\n")
    print(f"{'γ':>2} | {'vLLM tps':>9} {'加速':>6} {'AL':>5} | "
          f"{'mlx tps':>8} {'加速':>6} {'AL':>5} | AL差")
    print("-" * 72)
    for g in sorted(set(v) & set(m)):
        if g == 0:
            continue
        vc, mc = v[g], m[g]
        v_al, m_al = vc["AL"], 1 + g * mc["rate"]
        print(f"{g:>2} | {vc['tps']:>9.2f} {vc['tps']/base_v:>5.2f}× {v_al:>5.2f} | "
              f"{mc['tps']:>8.2f} {mc['speedup']:>5.2f}× {m_al:>5.2f} | "
              f"{(m_al-v_al)/v_al:>+6.1%}")

    print(f"\n逐位接受率 P(接受数 ≥ k)——**这是算法等价性的硬判据**")
    for g in sorted(set(v) & set(m)):
        if g == 0 or "P_ge_k" not in m[g]:
            continue
        vp, mp = v[g]["P_ge_k"], m[g]["P_ge_k"]
        n = min(len(vp), len(mp))
        print(f"  γ={g}")
        print(f"    vLLM : " + " ".join(f"{p:6.1%}" for p in vp[:n]))
        print(f"    mlx  : " + " ".join(f"{p:6.1%}" for p in mp[:n]))
        print(f"    差   : " + " ".join(f"{(mp[i]-vp[i])*100:+5.1f}pt" for i in range(n)))


if __name__ == "__main__":
    main()
