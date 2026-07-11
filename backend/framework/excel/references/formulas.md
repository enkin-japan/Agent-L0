# Which Excel functions to use (and which never to)

The delivery gate recalculates your file with LibreOffice Calc. A function it cannot
compute (or that openpyxl cannot serialize with spill metadata) will fail verification
even though it would work in desktop Excel. Stay inside the safe set.

## Safe core (use freely)

- Math/aggregation: `SUM SUMIF SUMIFS SUMPRODUCT COUNT COUNTA COUNTIF COUNTIFS
  AVERAGE AVERAGEIF AVERAGEIFS MIN MAX ROUND ROUNDUP ROUNDDOWN ABS MOD INT`
- Lookup (classic): `VLOOKUP HLOOKUP INDEX MATCH OFFSET CHOOSE` —
  prefer `INDEX(...,MATCH(...))` over VLOOKUP for left-lookups and robustness.
- Logic: `IF AND OR NOT IFERROR IFNA ISBLANK ISNUMBER ISTEXT`
- Text: `LEFT RIGHT MID LEN TRIM UPPER LOWER PROPER TEXT CONCATENATE SUBSTITUTE FIND
  SEARCH` (`&` for concatenation is fine)
- Date/time: `DATE YEAR MONTH DAY EOMONTH EDATE TODAY NOW WEEKDAY NETWORKDAYS DATEDIF`
- Financial: `NPV IRR PMT PV FV RATE NPER`

## Banned — dynamic-array / spill functions

`XLOOKUP FILTER UNIQUE SORT SORTBY SEQUENCE RANDARRAY TEXTSPLIT TOCOL TOROW
LET LAMBDA BYROW BYCOL MAP SCAN`

Two independent reasons:
1. openpyxl writes no spill metadata (`cm`/`vm` attributes), so even in real Excel the
   result may occupy only the anchor cell instead of spilling.
2. The verification engine may not compute them; you get a lowercased formula or
   `#NAME?` in the shadow and an L1 failure.

Replacement patterns:
- `XLOOKUP(a, col1, col2)`      → `INDEX(col2, MATCH(a, col1, 0))`
- `FILTER` / `UNIQUE` / `SORT`  → do it in Python (pandas) and write plain values,
  or use classic `SUMIFS`/`COUNTIFS` when the goal is aggregation.
- `SEQUENCE`                    → write the series from Python.

## Sometimes-needed `_xlfn.` prefix

Functions newer than Excel 2007 must be written with the `_xlfn.` prefix for the file
format, e.g. `=_xlfn.IFS(...)`, `=_xlfn.TEXTJOIN(",",TRUE,A1:A9)`, `=_xlfn.MAXIFS(...)`,
`=_xlfn.MINIFS(...)`, `=_xlfn.CONCAT(...)`, `=_xlfn.SWITCH(...)`.
Excel shows them normally. Without the prefix Excel displays `#NAME?`.
If unsure, avoid the function: every `_xlfn.` function above has a classic equivalent
(`IFS`→nested `IF`, `TEXTJOIN`→`&` chain or Python, `MAXIFS`→`MAX(IF(...))` avoided —
use SUMPRODUCT tricks or Python).

## General rules

- Formulas compute, Python prepares: sorting, dedup, reshaping, joins belong in
  pandas **before** cells are written; live arithmetic the user should be able to
  audit and re-run belongs in cell formulas.
- Anchor absolute references (`$B$5`) for single assumption cells; relative for
  fill-down ranges.
- Write 2–3 formulas first, verify they pull the intended cells, then fill the range.
