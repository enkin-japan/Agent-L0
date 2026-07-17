# markitdown 文件解析融合评估

> 2026-07-18 过夜评估任务 5/5。对象：`/Volumes/enkin/project/markitdown`
> （microsoft/markitdown，MIT，AutoGen 团队出品——各类文件→Markdown，供 LLM
> 消费）。评估问题：是否适合融入本项目文件解析。

---

## 1. 首要发现：它已经在本项目里服役了

**markitdown 不是候选，是现役。** `markitdown[docx,pptx,xlsx]==0.1.6` 已钉在
backend/requirements.txt 与 requirements-edge.txt（进 app bundle 依赖树，
pandas/magika/onnxruntime 等连带体积在 edge 瘦身账里都记过账），两个调用点：

- **摄入层**（`ingestion/parsers/document.py`）：办公文档「急解析」——上传的
  docx/pptx 等经 markitdown 转 markdown、12000 字截断、烘进持久化历史（解析
  一次不重解析）；xlsx/xlsm/xltx 例外，走自研 excel 结构摘要（比整表倾倒聪明）；
- **file_extract 工具**（办公文档分支）：模型按需取全文，同一个 parse_document。

且分工划得正确：markitdown 只管办公文档；PDF（自研 pymupdf：文本抽取 + 低文本
页渲染成图走原生视觉，正好覆盖扫描件）、音频（本地 whisper ASR）、图像（原生
直喂）都是自研路线——这三路每一路都比 markitdown 对应的转换器强（详见 §3）。

**版本**：本地开源仓快照 `__about__.py` = **0.1.6，与本项目钉的版本一致**——
无升级增量可取。

因此本评估的实际产出不是「要不要融入」，而是**查漏补缺**：对照它的完整能力面，
找出本项目没吃到的部分并逐一裁决。

## 2. 能力面对照（本项目吃了什么、没吃什么）

| markitdown 能力 | extra/包 | 本项目现状 | 裁决 |
|---|---|---|---|
| docx/pptx/xlsx | `[docx,pptx,xlsx]` | ✅ 现役 | 保持 |
| **.xls 旧版 Excel** | `xls`（xlrd） | **声明支持但实际会失败**（见 §3.1） | **建议补** |
| PDF | `pdf`（pdfminer） | 自研 pymupdf 更强（文本+渲染成图双路） | 不采用 |
| 音频转写 | `audio-transcription`（SpeechRecognition→云 API） | 本地 whisper | 不采用（隐私+质量双输） |
| 图像 EXIF/描述 | core + llm_client | 原生视觉直喂 | 不采用 |
| OCR 插件 | markitdown-ocr（LLM vision 抠文档内嵌图） | 扫描 PDF 已由「低文本页渲染成图」覆盖 | 不采用 |
| ZIP 遍历 | core | 未接（zip 走懒登记） | 可选小增益（§3.3） |
| YouTube 字幕 | `youtube-transcription` | media_download（yt-dlp）线覆盖 | 不采用 |
| Outlook .msg | `outlook`（olefile） | 未接 | 等真实需求 |
| Azure Doc Intelligence / Content Understanding | `az-*` | — | **永不**（云解析=数据出门，红线） |
| epub/html/csv/json/xml/ipynb | core 自带 | epub 已在 ext 表；html 有自研清洗线 | 保持 |

## 3. 裁决细节

### 3.1 名实不符的扩展名表 ★ 建议顺手修（本评估唯一的真发现）

`ingestion/router.py` 与 `file_extract.py` 的办公文档扩展名表声明了
`.doc .ppt .xls .odt .odp .ods .rtf`，但 markitdown **没有这些格式的本地转换器**
（实读 converters/ 目录证实：`.xls` 有 XlsConverter 但需 `xls` extra 的 xlrd；
`.doc/.ppt/.odt/.odp/.ods` 无转换器；`.rtf` 只有 Azure 云转换器）。现状是这些
文件会走到 parse_document 然后 fail-open 返回 `[Document parsing failed: …]`
——不崩，但模型看到的是「解析失败」而非「格式不支持，请转存为 docx」这样的
可行动指引。两个小修：

1. **补 `xls` extra**（requirements 两处 `markitdown[docx,pptx,xlsx]` →
   `[docx,pptx,xlsx,xls]`）：xlrd 极小、pandas 本就在依赖树，.xls 从假支持变
   真支持；
2. **其余摘出 ext 表**（`.doc .ppt .odt .odp .ods .rtf` 改走懒登记路——路径
   登记进上下文，模型可用 shell/textutil 转换后再取；macOS 自带 `textutil`
   可把 .doc/.rtf 转 docx/txt，这比假装支持诚实且给了出路）。

属行级改动 + 金测试补断言，与 S.3「降级告警要跟着能力宣告走」同一纪律。

### 3.2 三条自研路线维持不动（这是对的，不是欠账）

- **PDF**：markitdown 的 pdf extra 是 pdfminer 纯文本抽取——丢版面、无扫描件
  对策。本项目「每页文本 < 阈值即渲染成图走原生视觉」的双路设计严格更强；
- **音频**：SpeechRecognition 默认打云端 API（隐私红线）且质量不如本地 whisper；
- **文档内嵌图 OCR**：markitdown-ocr 插件要 OpenAI 兼容客户端，本项目扫描件
  需求已被 PDF 渲染路覆盖；办公文档内嵌图的文字抽取属长尾，等真实需求。

### 3.3 ZIP 遍历 ☆ 可选小增益，等需求

markitdown core 自带 zip 转换器（解包逐个转内容）。本项目 zip 现走懒登记。
用户若出现「发个压缩包让 Agent 读」的真实用法，在 parse_document 前加一个
`.zip` 分支即可（要配套解包体积熔断，防 zip 炸弹——参照 media_download 的
2GB 熔断纪律）。需求未现前不动。

## 4. 结论

「是否适合融入」的答案是**早已融入，且融对了位置**：markitdown 只承担它最强的
一段（办公文档→markdown），PDF/音频/图像三路保持更强的自研+本地实现，Azure 系
云解析永不引入。本地仓快照与现役版本同为 0.1.6，无升级动作。真正值得动手的
只有一件小事：**扩展名表名实校准**（补 `xls` extra + 假支持格式摘出改懒登记，
给模型可行动的出路）——行级改动，可与任一波顺手做。

*评估依据：markitdown packages/ 源码（converters 目录全扫、pyproject extras、
__about__ 版本、markitdown-ocr README）；对照本项目 ingestion/router.py、
parsers/document.py、file_extract.py、requirements 两处钉版。*
