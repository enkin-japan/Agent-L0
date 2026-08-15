# S3(iii) fast 判据初探（K%512 → K%256）——开跑前预测（2026-08-15）

工具：`benchmarks/bench_gather_fast_judge.py`，同一脚本两个 venv 各跑一遍：
`backend/.venv`（wheel 0.32.0：gather fast 判据 `K%512`，26b 三矩阵全落空走慢路）
vs `backend/.venv-mlxsrc`（main 自建：`K%256`，gate/up K=2816 走 `affine_gather_qmv_fast`，
down K=704 仍落空）。

## 口径与已知混杂（诚实声明）

- **这是初探不是归因**：两个 venv 差 702 行 quantized.cpp（main vs v0.32.0），
  任何差异都可能来自其它改动。初探显著（gate/up ≥8%）才值得做单变量隔离
  （同一 main 源码、只把判据改回 K%512 重建对照）。
- `.venv-mlxsrc` 构建含 T6a cap7 补丁——它只动稠密 `qmv_wide` 的瓦片数，
  **不碰 gather 路**，对本测无影响。
- E=128 真实形状，8 份独立层权重（足迹 8×808MB=6.5GB ≫ SLC），
  索引按真实 top-k 形态（每 token 8 个不重复专家）。

## 可证伪预测

- **P-F1**：gate/up（K=2816，fast 可及）在 mlxsrc 下**变快**，幅度 5%~30%
  （packs_per_thread 1→2、K 循环减半；若 gather_qmv 在 B 小时是发射/占用受限，
  收益会靠上限）。down（K=704，两版都落空）差异 ≤±5%（阴性对照）。
- **P-F2**：B=8（基线口径）与 B=32（验证批口径）同向。
- **P-F3**：换算：gate/up 占专家字节 2/3、专家占激活 37.3% ⇒ gate/up 提速 x%
  对基线解码 tps 的上界贡献 ≈ 0.25·x%（其余组件不变）；x=15% ⇒ ~+3.7% tps。
  初探若 ≥8% 即立项做隔离验证 + 真模型 A/B，<8% 记档收摊。

## 判读预先承诺

- down 阴性对照若也动了 ≥8%，说明差异来自 702 行里的别处，
  **不许**把收益记到 fast 判据头上，改判"main 整体更快/更慢"并另行归因。
