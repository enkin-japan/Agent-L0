# 真机演练观察存档（2026-08-22，阶段复审 L9 补档）

脚本只 print 不落盘,当时的观察值原样誊录于此(来源:主会话执行记录)。
复现方式:按各脚本 docstring 的前置(config 临时切换)重跑;本档案使离线核对成为可能。

## B2 代际围栏 · 基线场（HEAD=68746ba,期望复现缺陷）— drill_restart_fence.py --expect defect
```
{"pre_restart_deltas": 28, "generation_before": null, "generation_after": null,
 "generation_changed": false, "turn_ended_after_restart": false,
 "turn_state_after": null, "turn_generation_field": null, "last_history_status": "fail"}
判定: 缺陷复现（turn_ended 永远没来）✅
```

## B2 代际围栏 · 修后场（含复审修正,期望围栏生效）— --expect fixed
```
{"feed_generation_before": "126f4443041b4479", "pre_restart_deltas": 19,
 "generation_before": "126f4443041b4479", "generation_after": "97f52fef446d450c",
 "generation_changed": true, "turn_ended_after_restart": true, "turn_state_after": null,
 "turn_generation_field": "97f52fef446d450c", "feed_generation_changed": true,
 "feed_backfill_delivered": true, "last_history_status": "fail"}
判定: 会话流(代际+补发) + feed(代际变化+重锚后补发条目)均到达 ✅
```
（注:修后场第一次尝试因模型走工具路停在 awaiting_approval 被判据挡下,
判据放宽为 token+thinking 增量后重跑得上值——过程在台账波 B2 交付块。）

## D0③ catalog 逃生口 · 端到端（tool_schema_mode=catalog + inprocess）— drill_catalog_e2e.py
```
{"turn_ended": true, "status": "success",
 "shell_args": {"command": "echo catalog-ok-1787389404"}, "args_nonempty": true,
 "echo_output_seen": true, "used_get_tool_schema": false}
判定: catalog 模式带参调用端到端可用 ✅
```
（used_get_tool_schema=false 如实在案:该轮模型凭目录直调,方言渲染取详情
无端到端消费证据,只有确定性层金测试守——台账波 D0 交付块已如实界定。）
