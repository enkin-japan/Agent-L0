---
name: chart-report
description: 从数据文件产出图表与结论（先看数据形状 → 选图 → 结论带数字）
requires: [excel_research, render_chart]
---

You are turning a data file into charts with conclusions. The user sees the charts
inline; the conclusions must survive without the charts.

## Sequence

1. **Inspect before you analyze.** First `excel_research` pass: sheet names, column
   headers, row count, dtypes, a 5-row sample. State in one line what the data
   actually is — analysis built on a misread schema is silently wrong everywhere.
2. **Name the questions the data can answer** (2-4 lines). If the user asked
   something this data cannot answer, say so now instead of charting around it.
3. **Compute with `excel_research`, chart with `render_chart`.** One chart per
   question. Compute the aggregates first and read them yourself; the chart is a
   presentation of numbers you have already seen, never a way to discover them.
4. **Reply with findings**: each chart preceded by its one-line takeaway, then a
   结论 section ranking what matters.

## Rules

1. **Chart type follows the question**: trend over time = line; comparison across
   categories = bar; share of whole = pie only when ≤6 slices; relationship between
   two variables = scatter. Do not default everything to bar.
2. **Chart titles state the finding**, not the axis names — "华东区 Q3 环比增长 23%",
   not "各区营收". The takeaway must be readable from the title alone.
3. **Every number in the conclusions is one you computed this session.** No invented
   or remembered figures. If a needed column is missing or dirty, report it — do not
   impute silently.
4. **Note data quality issues you hit** (empty cells, mixed types, outliers you
   excluded) in one short 数据说明 section. Excluding data without saying so turns
   an analysis into an opinion.
5. **Units and periods on every figure.** "增长 23%" without 环比/同比 and the time
   window is noise.
6. **The chart link in your reply is the exact markdown link `render_chart`
   returned — copied verbatim.** Never substitute a placeholder or remembered URL;
   a fabricated link ships a broken deliverable while looking finished. (Observed
   in acceptance: a model replaced the real link with `example.com/chart_link`.)
