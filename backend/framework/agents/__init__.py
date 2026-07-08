"""
受限调查沙箱（方案 b：物理隔离）。

web_search / open_pages 这两个「脏数据工具」只在本包内部装配，绝不放进 framework/mcp/tools，
因此 MCPRegistry 的插件扫描永远看不到它们——主 Agent（挂着 shell/accessibility 等危险工具）
物理上不可能直接接触原始网页文本。主体唯一可见的是 mcp/tools/web_research.py 这层包壳：
它在内部跑一个只有 {web_search, open_pages} 的受限循环，复用同一份模型权重与推理引擎，
把多步搜索/抓取/交叉验证折成一份干净摘要，再交回主体。注入即使劫持了沙箱，也够不到任何
能执行动作的工具，最坏只能返回一份被污染的文本（事实污染），无法造成行为劫持。
"""
