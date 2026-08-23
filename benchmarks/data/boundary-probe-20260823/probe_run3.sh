#!/bin/zsh
# 第三段：B3「先列后算」探针（2026-08-23 晚，用户拍板先跑再修）。run_eval --ids 只认精确 id。
set -u
cd /Volumes/enkin/project/Agent/backend
OUT=/Volumes/enkin/project/Agent/benchmarks/data/boundary-probe-20260823
PYTHONUNBUFFERED=1 .venv/bin/python benchmarks/model_eval/run_eval.py --model gemma-4-26b-a4b-it-4bit --dims text \
  --ids tx-lcs-8k-table-list,tx-lcs-16k-table-list,tx-lcs-32k-table-list,tx-lcs-48k-table-list \
  --spec-mode off --out $OUT/gemma-4-26b-a4b-it-4bit-listfirst
rc=$?
echo "=== 26b-4bit table-list exit=$rc ==="
