---
name: research
description: 深度网页调研并产出带引用的调研报告（检索 → 交叉验证 → 落盘）
requires: [web_research, web_read, write_file]
---

You are producing a research report the user will rely on. The output is a markdown
file with cited sources — not a chat answer.

## Sequence

1. **Restate the question and scope in 2-4 lines before any tool.** Name what would
   make the report complete (the 3-6 sub-questions you will answer). If the scope is
   ambiguous, ask now — a report answering the wrong question wastes every step after.
2. **One `web_research` per sub-question**, not one giant query. Specific queries
   return usable sources; broad ones return listicles.
3. **`web_read` the 2-4 most load-bearing pages.** Search snippets are teasers, not
   evidence. Any claim that shapes a conclusion must come from a page you actually
   read.
4. **Write the report with `write_file`** using a plain filename like
   `research-<topic>.md` (a bare filename lands in the session workspace; the tool
   returns a download link — copy that link verbatim into your reply). Structure:
   结论先行 (the answers, 5-10 lines) → 各子问题分节 (evidence per sub-question) →
   来源清单 (numbered URLs).

## Rules

1. **Every factual claim carries a source marker** (`[1]`, `[2]`…) resolving to the
   来源清单. A claim you cannot source gets labeled 推测 explicitly or gets cut.
2. **Key conclusions need two independent sources.** Two pages citing the same press
   release are one source. If sources disagree, report the disagreement — do not
   silently pick one.
3. **Dates matter.** Note when a source is older than a year on fast-moving topics;
   prefer newer. Record the retrieval date in the report header.
4. **Numbers are quoted, not remembered.** Copy figures from the page text you read,
   with their units and time period. Round only in the 结论 section.
5. **Stop when the sub-questions are answered**, not when the search space is
   exhausted. If two rounds of searching add nothing new, write what you have and
   list what remains open under 未决问题.
