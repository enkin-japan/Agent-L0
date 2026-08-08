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

## Modern functions — allowed WITH the file-format prefix

Functions newer than Excel 2007 must be written with the `_xlfn.` prefix for the file
format; without it both real Excel and the gate show `#NAME?`. Gate-verified working
set (2026-08-09, values checked against LibreOffice recalc):

- Plain `_xlfn.` family: `=_xlfn.XLOOKUP(a,col1,col2)`, `=_xlfn.XMATCH(...)`,
  `=_xlfn.IFS(...)`, `=_xlfn.TEXTJOIN(",",TRUE,A1:A9)`, `=_xlfn.MAXIFS(...)`,
  `=_xlfn.MINIFS(...)`, `=_xlfn.CONCAT(...)`, `=_xlfn.SWITCH(...)`,
  `=_xlfn.UNIQUE(...)`, `=_xlfn.SEQUENCE(...)`
- `LET` uses `_xlpm.` for its variable names: `=_xlfn.LET(_xlpm.x,5,_xlpm.x*2)`
- Spill lookup/sort/filter need the longer worksheet prefix:
  `=_xlfn._xlws.SORT(...)`, `=_xlfn._xlws.FILTER(range,cond)`

Spill caveat: openpyxl writes no spill metadata (`cm`/`vm`), so a spill result may
occupy only the anchor cell in desktop Excel. **Wrap spill functions in an
aggregator** — `=SUM(_xlfn._xlws.FILTER(...))`, `=INDEX(_xlfn._xlws.SORT(...),1)`,
`=SUMPRODUCT(_xlfn.UNIQUE(...))` — or write the expanded values from Python.

## Still banned — the gate cannot evaluate them

`LAMBDA BYROW BYCOL MAP SCAN RANDARRAY TEXTSPLIT TOCOL TOROW SORTBY`
(inline `_xlfn.LAMBDA(...)(...)` recalcs to `#VALUE!` — verified). Classic
replacement patterns when you'd rather stay conservative:
- `XLOOKUP(a, col1, col2)`      → `INDEX(col2, MATCH(a, col1, 0))`
- `FILTER` / `UNIQUE` / `SORT`  → do it in Python (pandas) and write plain values,
  or use classic `SUMIFS`/`COUNTIFS` when the goal is aggregation.
- `SEQUENCE`                    → write the series from Python.

## General rules

- Formulas compute, Python prepares: sorting, dedup, reshaping, joins belong in
  pandas **before** cells are written; live arithmetic the user should be able to
  audit and re-run belongs in cell formulas.
- Anchor absolute references (`$B$5`) for single assumption cells; relative for
  fill-down ranges.
- Write 2–3 formulas first, verify they pull the intended cells, then fill the range.
