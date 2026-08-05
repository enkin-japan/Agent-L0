# Excel 底盘基准（2026-08-05，OfficeCLI 1.0.143 vs LibreOffice 影子重算，35 题）

输入形态=生产 verify 链真实形态：openpyxl 写裸公式、无缓存值。判分基准=手写真值
（expected 在题库里手算写死，不经任何引擎——两引擎都是被考者）。

| 指标 | LibreOffice（现役门禁） | OfficeCLI（候选） |
|---|---|---|
| 公式保真（35 题） | 33/35 | **35/35** |
| 全表取值耗时 | 1.7s（重算+回写） | **0.3s**（读取即算） |
| 视觉回读 | **1.9s**（PDF→PNG，2 页） | 2.7s（HTML→无头浏览器截图） |
| 外部依赖 | soffice + poppler | 单二进制（截图需浏览器） |

LO 仅有的两败：XLOOKUP / TEXTJOIN → #NAME?。**根因不是 LO 弱，而是输入文件坏**：
openpyxl 裸写新式函数名（无 `_xlfn.` 前缀）不合 OOXML 规范，真 Excel 打开同样
#NAME?；补 `_xlfn.` 前缀后 LO 双双算对（已实证）。OfficeCLI 对此**宽容评估**
（README 明言 `_xlfn.` auto-prefix）——裸名也照算。

## 结论（2026-08-05）

1. **门禁保持 LibreOffice，不换**。门禁的职责是"预言用户 Excel 打开后看到什么"，
   LO 的 #NAME? 恰是对坏文件形态的忠实还原；OfficeCLI 的宽容反而会放过一类
   "验证通过、用户打开却 #NAME?" 的事故。加上写侧若未来迁 officecli，同引擎
   写/验自证的共模风险——独立实现正是门禁的价值。**架构判定：现役门禁无缺陷，
   无需回滚任何东西（本波基准先行、未动门禁）。**
2. **OfficeCLI 公式引擎保真合格**（35/35，含错误传播/链式依赖/OFFSET/INDIRECT/
   跨表），且快 5×。合格用途=对"预言 Excel 行为"无要求的场景：读取工具、
   沙箱修复循环的**快速预检**（NO-GO 前的廉价迭代反馈）——是否加装记生产机
   优化清单，本机不为 1.4s 差距加复杂度。
3. **视觉回读不换**：LO 链更快（1.9s vs 2.7s）且已有优雅降级；officecli 截图
   反而多一层无头浏览器依赖。
4. **附带发现（记观察清单）**：沙箱模型若写 XLOOKUP 等新式函数，今日门禁会
   如实 NO-GO（#NAME?）逼模型改写老函数——行为安全但浪费修复轮次；教沙箱
   提示词写 `_xlfn.` 前缀可解锁新函数族，属提示词工程，等生产机连同修复预算
   一起调。

复跑：`backend/.venv/bin/python backend/benchmarks/officecli_eval/run_officecli_eval.py`
