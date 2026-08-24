#!/usr/bin/env python3
"""把 rerun_<tag>/<model>/transcripts.jsonl 里指定题的修订版记录替换进 q/<model>/transcripts.jsonl。

用法：python3 merge_item.py <item_id> <rerun_dir_name>
替换而非追加：同一题只保留一条（修订版题面）；原始记录挪到
q/<model>/superseded_<item_id>.jsonl 追加留痕。随后跑 rescore.py 统一尺。
"""
import json
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent


def main(item_id: str, rerun_dir: str) -> None:
    merged = 0
    for mdir in sorted((DIR / rerun_dir).iterdir()):
        if not (mdir / "transcripts.jsonl").exists():
            continue
        new = [json.loads(l) for l in (mdir / "transcripts.jsonl").open()
               if json.loads(l)["item_id"] == item_id]
        if len(new) != 1:
            raise SystemExit(f"❌ {mdir.name}: rerun 里该题记录数 {len(new)} ≠ 1")
        qt = DIR / "q" / mdir.name / "transcripts.jsonl"
        recs = [json.loads(l) for l in qt.open()]
        old = [r for r in recs if r["item_id"] == item_id]
        if len(old) != 1:
            raise SystemExit(f"❌ {mdir.name}: 档案里该题记录数 {len(old)} ≠ 1")
        if old[0] == new[0]:
            print(f"{mdir.name}: 档案已是 rerun 版（重复并档？），跳过，不追加 superseded")
            continue
        with (DIR / "q" / mdir.name / f"superseded_{item_id}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(old[0], ensure_ascii=False) + "\n")
        recs = [new[0] if r["item_id"] == item_id else r for r in recs]
        qt.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
        merged += 1
        print(f"{mdir.name}: {item_id} {'✅' if new[0]['score'].get('pass') else '❌'} 替换完成")
    expected = len([d for d in (DIR / "q").iterdir() if d.is_dir()])
    if merged == 0:
        raise SystemExit("❌ 一个档案都没并（rerun 目录空/全重复？）")
    print(f"并档 {merged}/{expected} 档（缺席的档位=该次 rerun 未跑，须人工确认是否故意）")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
