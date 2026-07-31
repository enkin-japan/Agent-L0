# L0 出网守卫脱敏质量评测（l0_eval）

**这是什么**：`framework/cloud/egress_guard.py`（L0）的脱敏质量基线——出网 payload
里的个人标识遮没遮住（召回）、不该遮的遮没遮（过遮）。**L0 选型与放宽人工审批闸
（config `cloud.require_approval`）的依据是这里的实测数字，不是直觉。**

## 跑法

```bash
# 仓库根；每模型一跑（引擎直连 mlx 进程内，与部署同参）
backend/.venv/bin/python backend/benchmarks/l0_eval/run_l0_eval.py --model Qwen3.5-9B-4bit
```

## 口径

- **走生产同路**：逐题过 `egress_guard.sanitize`（规则腿 + 模型腿），与
  cloud_consult 出网前完全同一条代码路径——测的是"部署里那个 L0"，不是裸模型。
- **判分纯规则**：`must_mask` 逐字串必须从脱敏文本消失；`must_keep` 必须保留。
  无 LLM-judge、无加权总分（model_eval 同一纪律）。
- **题库**（`items.jsonl`，三语合成个人数据，鉴定纪律 fit 字段必填）七形态：
  name/address/org/account（规则腿够不着的语境标识）、mixed（双腿协同）、
  injection（payload 内注入话术——结构隔离下应照遮不误）、negative（零可遮，
  过遮探针：技术问题被遮烂 = 可用性破产）。
- **指标**：item_pass_rate（题全对率）/ span_recall（标识召回）/
  over_mask_items（过遮题数）/ model_leg_ok_rate / latency 中位。

## 基线

见 `BASELINE.md`（每次接受新水平时回填；results/ 产物不入库——轨迹含合成
个人数据，虽为虚构仍按真数据纪律对待）。

## 边界（诚实记录）

- 题库是合成数据：真实使用中的漏报形态（生僻称呼、方言昵称、上下文暗指）
  只能靠出网台账复盘补题——题库随真实 badcase 扩充。
- 规则腿的模式集是保守起步（email/电话/IP/密钥/卡号 Luhn）；My Number 等
  12 位纯数字因误杀率放弃通用规则，交模型腿。
- 判分只看"遮没遮"，不看"遮完之后云端建议还有没有用"（占位符语义保持度）——
  后者等真 key 联调后按实际建议质量补维度。
