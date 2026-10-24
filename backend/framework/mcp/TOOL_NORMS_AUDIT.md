# 工具行为准则审计（#83，2026-08-10）

> **这是什么**：全部 61 个工具的行为准则逐件审计——哪些描述缺了 26B 档需要的
> 行为约束、缺什么、改了什么、怎么验收。用户拍板动机（2026-08-10）：以 web search
> 语言选择为例——中文对话让查欧美新闻，模型下意识用中文关键词搜，中文圈二手
> 转述以讹传讹损失精度；要求多语言检索势必更准。推而广之全工具。
>
> **审计原则（L4 站实证，2026-08-09）**：
> 1. **描述是行为契约，按 26B 的理解力写**——31B 能自行桥接没写的常识，26B 不能
>    （skill_invoke 只写英文线索例→中文点名不触发 2/5，补例后 5/5；cloud_consult
>    不枚举供应商→幻觉 provider=google；三案同根）。31B 只会觉得啰嗦，不受害。
> 2. **具体例句/枚举 > 抽象训令**——"be multilingual" 没用，"news about the US →
>    English queries" 才起作用。
> 3. **每条准则配行为测试**，调到过为准，不搞坐而论道的提示词工程；同时配
>    **反向准则**防过度矫正（简单题不许加戏）。
> 4. **成本纪律**：工具 schema 每轮随 prompt 发（前缀缓存吃掉大半但上下文预算仍占），
>    只给「错误代价高 + 已观测/可预判跑偏」的工具加准则，不搞全员大扩写。
>
> **在位测试**：`tests/test_tool_norms.py` 钉关键准则存在性（改措辞可以，丢语义即红）。
> **真机探针**：`benchmarks/tool_norms/norms_probe.py`（4 用例，需活栈+真联网，26B 档跑）。

## 本轮修改（7 处，2026-08-10）

| 位置 | 改动 | 动机 |
|---|---|---|
| `agents/research_sandbox.py` SANDBOX_SYSTEM_PROMPT | 新增 Rule 8：**按信源社区语言检索**（美欧→英文、日本→日文、中国大陆→中文例）；跨区域话题**英文+本地语双查交叉**；跨语言口径不一致时**主源/原文优先并报告分歧**；纯本地题**不许加戏**；digest 用问句语言、专名原文保留 | 检索关键词真正诞生在沙箱；二手转述漂移是用户点名的核心痛点 |
| `agents/research_sandbox.py` READER_SYSTEM_PROMPT | 新增 Rule 5：专名/法规名/产品名/数字**原文保真**，必要处括注翻译 | 读取摘要层翻译丢精度、摘要没法对回原页核验 |
| `tools/web_research.py` 描述 | 问句语言随意**不要预翻译**（沙箱自选检索语言）；时效题把时间范围写进问句；终答语言跟用户 | 主模型别替沙箱做糟糕的预翻译；时间限定是时效精度的第二根柱 |
| `tools/rag_search.py` 描述 | **纠错**：删 "questions that require external sources for verification"（这是 web_research 职权，26B 照字面会拿本地库答新闻题）；改为用户本地知识库明确定位 + 公网事实指回 web_research；未命中**换词重试一次**（同义词/文档语言）再下"没有"结论 | 描述性 bug；检索未命中即弃是向量检索常见假阴性 |
| `tools/doc_write.py` 描述 | 文档语言跟**用户对话语言**（除非用户指定），他语材料翻译进交付物、专名原文并列 | 站 B「语言漂移③」毛病的工具级预防（31B 消失、26B 档要防复发） |
| `tools/render_chart.py` 描述 | 删 "may be in any language"，改为标题/标签跟用户对话语言，数据文件语言不外漏 | 同上；旧文案等于明示可以漂移 |
| `tools/excel_research.py` 描述 | 任务书里写明工作簿可见文本语言：**新建跟用户、编辑跟原簿** | 子模块看不见对话，语言约束必须写进任务书；编辑场景反向约束防把用户的英文簿汉化 |

## 全工具清单审计（61 件）

判定:✅=已达标不动 / 🔧=本轮已改 / ⏳=候选（有预判跑偏但无实证，等观测再动，原则 4）

### 检索与外部信息
| 工具 | 判定 | 依据 |
|---|---|---|
| web_research | 🔧 | 见上表。原描述已有隐私（不贴用户任务原文/个人细节）、重活纪律（一次一题）、防注入——保留 |
| web_read | ✅ | 主描述职权清晰（只读给定页、不搜索、防注入）；保真准则加在读取子模块提示词层（Rule 5）更对位 |
| rag_search | 🔧 | 职权纠错 + 重试准则，见上表 |
| rag_get_context | ✅ | 机械取邻块，参数来源已写死（rag_search 结果头），无行为分叉 |
| cloud_consult | ✅ | 昨日已修（有钥供应商枚举/空正文响亮/密钥剥离指引），L4 站 E2E 验证过 |

### 交付物产出
| 工具 | 判定 | 依据 |
|---|---|---|
| write_document | 🔧 | 输出语言准则，见上表 |
| render_chart | 🔧 | 同上；原有「真数据不编造/数学函数自算不算编造」双向准则很好，保留 |
| excel_research | 🔧 | 任务书语言条款，见上表；原有 verbatim 传递/GO 门禁/不粉饰失败准则完备 |
| deck_init / deck_set_slide / deck_status / deck_remove_slide / deck_export_pdf / deck_export_pptx | ✅ | 页面语言由 deck 剧本与对话主导；deck_set_slide 描述已含槽位契约；语言漂移③在 31B 判定中已消失，26B 档若复发再在 deck 剧本层加语言条款（剧本比 6 个工具描述更省 token——原则 4） |
| deck_critique | ✅ | 评审维度/verdict 契约已写死，一次性沙箱正文 JSON 兜底已修（7cbe011） |

### 计算
| 工具 | 判定 | 依据 |
|---|---|---|
| run_math | ✅ | 本项目准则写法的标杆：沙箱值压倒记忆、ASCII、符号/数值 API 不混用、自验、职权分流到 math_research |
| math_research | ✅ | ANSWER 逐位引用准则完备 |
| code_explore | ✅ | 模式枚举+职权（语义图 vs 文本搜）清晰 |

### 文件与系统（审批闸兜底型）
| 工具 | 判定 | 依据 |
|---|---|---|
| run_shell_command | ✅ | 读写分流/自动审批弹窗/禁 heredoc 已写；错误代价由审批闸兜底 |
| write_file / edit_file | ✅ | 小步编辑/唯一锚点/禁行号前缀/审批，昨日 R2b 测试中零漂移 |
| file_extract / find_files / list_dir / copy_file / move_file / trash_file / open_path | ✅ | 机械操作,职权与模式已写清；trash 明示可恢复 |
| file_manage 三件的「不覆写」语义 | ✅ | 描述已明示 Never overwrites |
| media_download | ✅ | 只收用户 URL/拒自造/拒歌单/先 probe——枚举式准则已完备 |

### 个人数据与系统信息
| 工具 | 判定 | 依据 |
|---|---|---|
| memory_write | ✅ | DO NOT save 清单+反向准则（存疑不写）是全清单最完备的一份 |
| rag_insert_document / rag_upsert_category / rag_list_categories / rag_reclassify_unclassified | ✅ | 三步流程/先查再建/拒绝即返目录，站 A 验证过 |
| rag_delete_category / rag_delete_document / rag_mark_stale | ✅ | 直调不口头求权（审批闸自动弹）准则已统一 |
| get_current_time / get_location / system_status | ✅ | 调用时机（何时不用调）已写 |
| clipboard_read / clipboard_write | ✅ | 只在用户指涉剪贴板时读/绝不轮询——隐私准则在位 |
| calendar_add / calendar_list / reminder_add | ✅ | calendar_add 已有「缺细节先问再猜」；写操作有审批 |
| notify_user | ✅ | 「不替代对话回答」反向准则在位 |

### 桌面自动化
| 工具 | 判定 | 依据 |
|---|---|---|
| run_applescript | ✅ | 审批闸+描述已含用途边界 |
| read_ui_tree / click_ui_element / set_ui_element_value / type_text / press_keys / list_applications | ⏳ | 无行为准则层面的观测跑偏；辅助功能自动化整链在 DEFERRED 候补，等真用起来再审计（原则 4） |

### 编排与元工具
| 工具 | 判定 | 依据 |
|---|---|---|
| skill_invoke | ✅ | 昨日三轮调参定稿（多语言线索例+任务映射例+次序令，5/5） |
| register_api_tool / register_skill | ✅ | 密钥红线/撞名准则在位；「待确认」句 08-28 按 § 减法与预算 删除（execute 响应自证） |
| schedule_create / update / delete / list | ✅ | 审批+先 list 再改的次序准则在位 |
| api_tool_factory 合成工具 | ✅ | description 质量准则已挂在注册面（"工具好不好用九成在这句话"校验文案） |

## 明日 L4 验收（与 #79 同场）

```bash
./colab/legacy/daily.sh start && ./devctl.sh start all   # 路径为 2026-10-24 迁移后
cd backend && .venv/bin/python benchmarks/tool_norms/norms_probe.py \
    --work /tmp/normswork --out /tmp/norms_results.json
```
通过线：4/4。跑偏则按 L4 站方法迭代描述（具体例句优先）至全过，产物归档
`benchmarks/model_eval/results/`（gitignored）。

## § 减法与预算（D2-减法半，2026-08-28）

> 背景：D0 真账（`scripts/tool_token_audit.py`）实测 full 模式 **12185 真 token，已越
> D4 拍板的 12000 线**。用户裁决（D.25）：减法先行，回不去再议重锚。

**三条纪律**（原则 4 的执行细则，删句只认这三条）：

1. **no-op 判据绑定生产档模型**（现 26b-4bit）。换生产档，所有「可删」结论作废重判。
2. **判为可删的，删整句不改词**——改词是新的提示词工程，要走完整的准则调优流程。
3. **可删仅限两类「不需要模型就能判」的**，其余一律只进下方观测清单、等真机套件搭车裁决：
   - **类 A｜代码已确定性强制事实的复述**（读源码即可证：该行为无论模型知不知道都
     机械发生，且事实在使用时刻由响应文案自证）；
   - **类 B｜同一稳定前缀内的重复表述**（同声明内参数述与主述近字节重复、或跨声明
     字节级重复；删其一后信息仍在场）。

**本轮删句台账（12 处，full 12185 → 11887，−298 token）**：

| 位置 | 删句 | 类 | 代码证据 |
|---|---|---|---|
| run_shell_command 描述 | 响应格式句（reports stdout/stderr/exit code…denied） | A | stdout/stderr 由 execute 条件拼装（空则 "(no output)"）；拒批文案由编排层产出（orchestrator "Operation denied by user…"）——两路都在使用时刻抵达模型 |
| run_shell_command.timeout | 钳位机理句（orchestrator abandons after 180s…） | A | `_clamp_timeout` 强制 + 跨模块契约测试；保留 max 与 capped 事实 |
| register_api_tool 描述 | 不立即生效/落待确认区句 | A | `execute` 返回「已提交待确认…不会立即生效…」（register_tools.py:122） |
| register_skill 描述 | 同上 | A | 同上（同文件 skill 分支） |
| run_applescript 描述 | The response returns the script's output or the error | A | execute 返回即是 |
| edit_file 描述 | heredoc 禁令副本 | B | 主本留 run_shell_command（事故发生地，26B heredoc 写炸实案）；heredoc **非**代码强制（预检见 `<<` 即跳过），故按类 B 去重而非类 A |
| write_file 描述 | heredoc 禁令副本 | B | 同上 |
| memory_write.type 参数述 | "user \| feedback \| project \| reference" | B | enum 同声明相邻，四类型语义主述已详释 |
| math_research 描述 | 自足题面句（Pass a complete, self-contained…） | B | problem 参数述近字节同义，留参数侧 |
| excel_research 描述 | 自足任务书句 | B | task 参数述近字节同义；语言条款原样保留（金测试钉） |
| schedule spec 述 | (weekday 0=Monday..6=Sunday) 括注 | B | weekday 字段述逐字同义 |
| schedule_update.spec 述 | 整块换短述指回 create | B | full 模式同一前缀渲染两遍；`_SPEC_SCHEMA_UPDATE` 浅拷贝仅换 description，properties 同引用零漂移。**catalog caveat**：catalog 模式只留描述首行且丢 parameters，指回对象不在场（子字段述仍随 get_tool_schema 取到，仅丢 JSON 形状例）——切 catalog 前重估本条 |

**减法候补观测（疑似 no-op，需模型行为裁决，等下次真机套件搭车；不删。与文末「后续观测清单」分工：这里只管删句候补）**：
update_plan.op 的语义映射行；schedule_update/write_file/edit_file 的「Requires/The user
approves…」句（防口头求权准则的一部分，删除可能复发 26B 口头求权）；deck_export_pdf 的
「Requires ≥1 slide」；deck_set_slide 的 "(generation arrives later)" 括注。
cloud_consult 整件不动（⑤′ 刚调优）。另挂一条**已删句的回填监测**：register 双件的类 A
依据是**中文**响应文案替代英文描述句，26B 跨语理解未实测——真机套件搭车验证，跑偏即回填。

**第二批删句（D1 金测试复审 C3 迫出，2026-08-28 当日，3 处 −35 token）**：

| 位置 | 删句 | 类 | 代码证据 |
|---|---|---|---|
| update_plan 描述 | "(limited number of re-plans)" | B | task.render 每轮渲染 "re-plans N/max"，信息在场 |
| find_files 描述 | "Returns up to 30 matches, newest first." | A | 输出首行自报 "N matches (showing M, newest first)" |
| memory_write.valid_until | EXPIRED 机理句 | A | notes.py 把 [EXPIRED …] 渲染进模型可见索引行；原被 test_temporal_and_pointers 钉住，钉字同步改指存留准则半（Set ONLY/Omit durable） |

**⚠️ 口径订正（复审 C3，比第二批更重要）**：第一批的「11887 回线下余量 113」是
**裸类形口径**——生产装配的 skill_invoke 带 repo 剧本索引（5 份，+134 token），真实
前缀当时仍在 12021 越线。`tool_token_audit.py` 与预算闸金测试已同步改为**生产形口径**
（密封构造：无云钥、无活 RAG、repo 剧本索引经 AST 名单探活装配）。

**预算现状（生产形口径，D.26 重锚后）**：full **11986 / 12300，余量 314 ≈ 1.5 件中等
工具**。线值与两把尺之争已由用户裁定（2026-08-28，§D D.26）：预算门认**真 token 尺**
（`tool_token_audit.py`/金测试同一实现），est（chars/4）退役为摘要阈值内部参数不再当门；
原 D4 的 est 12k 线（07-17 档案：超线切 catalog）随之取代。越线阶梯不变：再减法 →
议重锚（用户）→ catalog（最后）。批量扩容（D2 第三批工具类）仍会撞门，届时捆绑评估
catalog。本轮**类 B 删句 8 处已获用户追认保留**（D.26；监测兜底照旧）。

## 后续观测清单（⏳ 触发条件）

- deck 页面语言漂移在 26B 档复发 → deck 剧本加语言条款（不动 6 个工具描述）
- 辅助功能链启用 → accessibility 六件补准则审计
- rag_search 换词重试准则的实效（假阴性率）→ 生产观测后决定是否升级为
  「先中后英双查」硬准则
