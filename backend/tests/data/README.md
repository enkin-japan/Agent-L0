# 金测试快照数据

- `model_visible_text.snap`:模型可见稳定文本快照(工具声明方言渲染 + DEFAULT_SYSTEM_PROMPT)。
  **只能**由 `UPDATE_MODEL_VISIBLE_SNAPSHOT=1 pytest tests/test_model_visible_text.py`
  重新生成,**绝不手改**;重生成后把 git diff 纳入评审(期望值由已提交代码重生成、
  人审 diff——deepseek refresh 态路数,惯例源头见 protocol/fixtures/README.md)。
  快照为**密封构造**形态(无云钥、无活 RAG、repo 剧本索引),与环境无关,
  不含任何用户数据——若 diff 里出现供应商名/知识库类目,那是密封被打破,先修密封。
