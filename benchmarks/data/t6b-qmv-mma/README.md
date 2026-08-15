# t6b-qmv-mma 原始件说明（08-16 复审后补，档案告诫优先于数据本身）

T6b（`affine_qmv_mma` simdgroup-matrix 内核）的全部实测原始件。
判定正文见 `benchmarks/predictions/prediction_T6b.md`；内核补丁
`benchmarks/patches/t6b_qmv_mma.patch`（=mlx-src 分支 `t6b-qmv-mma`@74ed440）。

## ⚠️ 构建指纹告诫（先读这个再用数据）

**本目录 15 份 JSON 里 12 份的 `mlx` 字段是 `0.32.1.dev20260815+adf21de`——
那是不含 qmv_mma 内核的上游基线 hash，却带着 `arm="mma"` 的数。**
原因：`mx.__version__` 的 git hash 取自**已 commit** 的源码，当晚内核在
工作树上迭代、20:38 才首次 commit（b1c292d），此前所有构建的指纹都停在
adf21de。这正是 §4.32 记的教训的另一面：**源码不 commit 时，版本串指纹
无区分力；`.so` mtime 才是硬指纹**——而本目录文件的 mtime 是拷贝时间，
同样不可考。后果：

- 任取两份 `+adf21de` 文件，无法从档案确认它们是否出自同一个二进制；
- 只有 3 份 `+b1c292d`（`*_v6.*`，21:04 落盘）可锚定为"20:38 commit 后构建"。

**当前 `.venv-mlxsrc` 安装件已另行判明**（08-16，两个独立行为判据）：
dist-info 写 `+b1c292d`，但实测 M=13/20/32 与禁用态逐位相同（`M<=12` 上界
只存在于 74ed440）、且 `MLX_DISABLE_QMV_MMA=""` 被当未设（同为 74ed440 语义）
——**装的是 74ed440 评审修复版**，版本串滞后一个 commit。

## 文件清单与已知局限

| 文件 | 内容 | 局限 |
|---|---|---|
| `tile4_*.json` / `tile8_*.json` | `bench_mlx_tile.py` 口径的 4bit/8bit 瓦片壁钟（31B 四形状×60 层，中位 25 次）；文件名尾即臂名（wide / mma / mma_v2 / v5 / e1 / final / v6） | 正文说的"四个结构变体（2SG-16行 / T fragment+双累加器 / 4SG 分 K / fp16 组内累加）"与这些文件名的**对应关系没有留档，已不可考**。可复算的是：M=6 六份极差 7.1%、剔除 e1（fp16 累加、按设计更快且已判死）后四份 4.4%——正文"1-4%"按此改口，见 prediction_T6b.md |
| `tile*_e1.json` | fp16 组内累加变体 | 数值上已判死（f16 误差劣于 wide），耗时仅作参考 |
| `num_*.json` | 对 fp32 参照（`mx.dequantize` 后 `matmul`）的相对 L2，`arm`/逐 (M,形状) 行 | **产生它的私有工具未存档**（会话级 scratchpad 已失）；复算需按上述口径重写。`num_e1` 与 `num_wide_m13` 用的不是同一份随机输入（未配对比较，见 prediction_T6b.md P5 修注） |
| `num_*_m13.json` 的 M=13 行 | 与 wide 臂逐位相同 | 这是**派发护栏（`M<=12`）生效的证据，不是 MMA 在 M=13 的数值证据**——M=13 根本没进 MMA 内核 |
| `scan31b4_*.log` | 端到端定档扫描（P6 的唯一原始证据；08-16 前曾被 `.gitignore` 的 `*.log` 吞掉未入库） | wide/hybrid 两臂、d8=8bit 草稿件；`v6confirm` 是 v6 构建的复确认 |
| 2bit | **无任何文件** | 派发条件放行 bits==2，但 2bit 壁钟从未测过——正文"+5~30%"仅由 4/8bit 支撑 |
