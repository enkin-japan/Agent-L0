#!/usr/bin/env python3
"""把 rerun020/<model>/transcripts.jsonl 里的 tc-real-020 修订版记录替换进 q/<model>/transcripts.jsonl。

替换而非追加：同一题在档案里只保留一条（修订版题面）；原始记录整体挪到
q/<model>/superseded_tc-real-020.jsonl 留痕。随后由 rescore.py 统一重判出 summary_rescored.json。
"""
import json
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent

for mdir in sorted((DIR / "rerun020").iterdir()):
    new = [json.loads(l) for l in (mdir / "transcripts.jsonl").open()]
    new = [r for r in new if r["item_id"] == "tc-real-020"]
    assert len(new) == 1, (mdir.name, len(new))
    qt = DIR / "q" / mdir.name / "transcripts.jsonl"
    recs = [json.loads(l) for l in qt.open()]
    old = [r for r in recs if r["item_id"] == "tc-real-020"]
    assert len(old) == 1, (mdir.name, len(old))
    (DIR / "q" / mdir.name / "superseded_tc-real-020.jsonl").write_text(
        json.dumps(old[0], ensure_ascii=False) + "\n", encoding="utf-8")
    recs = [new[0] if r["item_id"] == "tc-real-020" else r for r in recs]
    qt.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    print(f"{mdir.name}: tc-real-020 {'✅' if new[0]['score'].get('pass') else '❌'} 替换完成（原记录留痕 superseded）")
