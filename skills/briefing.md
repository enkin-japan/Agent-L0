---
name: briefing
description: 建立定时简报（对齐主题与节奏 → 查重 → 写自足的调度提示词）
requires: [schedule_create, schedule_list, get_current_time, web_research]
---

You are setting up a recurring briefing. The scheduled run executes with NO memory
of this conversation — the schedule's prompt is the entire specification it gets.

## Sequence

1. **Pin down three things before creating anything**: topic scope (what's in,
   what's out), cadence (每天/每周几点，用户时区), and depth (三条要闻 vs 深度综述).
   Missing any one, ask — a briefing that guesses these gets deleted within a week.
2. **`get_current_time` to anchor the timezone**, then **`schedule_list` to check
   for overlap.** If a similar briefing already exists, show it and ask whether to
   modify or add — two overlapping briefings deliver the same news twice.
3. **Do one manual dry run**: run the intended search (`web_research`) now, format
   the result as the briefing would look, and show the user. Adjust scope on their
   reaction — cheaper to fix now than after three scheduled deliveries.
4. **`schedule_create`** with the agreed cadence and a self-contained prompt, then
   confirm to the user: name, next run time, and what will arrive.

## Rules

1. **The scheduled prompt must be self-contained.** It names the topic, the search
   queries to run, the output format, and the length cap — "如上所述" or "按用户
   偏好" reference nothing at run time. Write it as instructions to a stranger.
2. **Order the briefing output by importance, cap it** (e.g. 5 items, one line each
   + one-paragraph 重点). An unbounded briefing trains the user to skip it.
3. **Instruct the scheduled prompt to state "本期无重大更新" when搜索无新内容** —
   padding a thin news day with filler erodes trust in every future delivery.
4. **One briefing per topic.** Merging unrelated topics into one schedule makes it
   impossible to adjust or delete one without losing the other.
