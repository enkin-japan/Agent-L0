# tool_call 借题鉴定日志

## 批次：BFCL v3（2026-07-30）

来源：`gorilla-llm/Berkeley-Function-Calling-Leaderboard`（HF dataset），
splits：simple / multiple / live_simple 各审前 25 条，**候选 75 → 采纳 13**（bfcl.jsonl）。
期望编译规则：possible_answer 候选值列表 → 单值 `eq` / 多容 `in` / 含空串 `optional`；
工具声明 `dict→object`、`float→number`。原始数据不入库。

### 采纳面（每题 fit 见 bfcl.jsonl source.fit）

类型基线（int/bool/数组/枚举）、同域多工具细粒度选择 ×3、操作型工具+克制诱导、
日期与数组多容、真实用户语料 ×4（含中文 1 道）、单位换算、格式服从探针。

### 拒绝类别（严格鉴定的证据）

| 类别 | 代表例 | 理由（对照 README 鉴定五问） |
|---|---|---|
| 纯数学 API 空转题 | simple_1..24 (factorial/gcd/quadratic 等) | 场景不贴合：与本项目工具面无映射；且模型可能心算后合理拒调，诱发 no-call 歧义（保留 simple_0 一道作类型锚点即可，同类 20+ 道全拒） |
| 表达式字符串参数 | simple_13–16 (calculus "x^2") | 判分脆：表达式写法多变（x^2/x**2/pow），possible_answer 覆盖不全 |
| 嵌套 dict 参数 | multiple_8 (budget{min,max}), multiple_9 (gradeDict) | 判分需递归字典语义匹配，改写脆、易误判 |
| 知识型 trivia 冗余 | multiple_18/19/22 (战史/教会/NBA 纪录) | 同域选择面已由 multiple_2 覆盖；知识面时效风险高 |
| 专业领域 API | multiple_7/16 (生态学建模) | 场景不贴合：本项目无同构工具形态 |
| 对抗性烂命名 | live_simple_22–24 (ChaFod/TheFod) | 测的是对故意混淆 schema 的容忍；自家工具命名受控，此面无路由价值 |
| 自由文本 verbatim 参数 | live_simple_20/21 (modification_request) | 判分需精确匹配长自由文本，脆 |
| 同模板复读 | live_simple_5..19 (get_current_weather ×15) | 同一工具同一考点重复；取 4-3-0（英文口语）与 3-9（中文）两道代表 |

### 已知取舍

- `live_simple_1` repos 参数要求精确逗号拼接（无空格）——判分严格是**有意的**：
  它测 schema description 的格式服从，格式偏差在真实 API 就是调用失败。
- `multiple_5` 坐标数组用 `eq` 精确匹配：值来自提示原文，测转录保真非计算。
