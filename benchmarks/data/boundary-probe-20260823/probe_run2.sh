#!/bin/zsh
set -u
cd /Volumes/enkin/project/Agent/backend
OUT=/Volumes/enkin/project/Agent/benchmarks/data/boundary-probe-20260823
THINK=tx-lcs-8k-table-think,tx-lcs-16k-table-think,tx-lcs-32k-table-think,tx-lcs-48k-table-think
TABLE=tx-lcs-8k-table,tx-lcs-16k-table,tx-lcs-32k-table,tx-lcs-48k-table
echo "[$(date +%H:%M:%S)] === 26b-4bit table-think start ==="
PYTHONUNBUFFERED=1 .venv/bin/python benchmarks/model_eval/run_eval.py --model gemma-4-26b-a4b-it-4bit --dims text --ids $THINK --spec-mode off --out $OUT/gemma-4-26b-a4b-it-4bit-think
rc=$?   # 复审修正：见 probe_run.sh 同注
echo "[$(date +%H:%M:%S)] === 26b-4bit table-think exit=$rc ==="
echo "[$(date +%H:%M:%S)] === qwen38-4bit table+think start ==="
PYTHONUNBUFFERED=1 .venv/bin/python benchmarks/model_eval/run_eval.py --model Qwen3.8-27B-4bit --dims text --ids $TABLE,$THINK --out $OUT/Qwen3.8-27B-4bit
rc=$?
echo "[$(date +%H:%M:%S)] === qwen38-4bit exit=$rc ==="
echo "[$(date +%H:%M:%S)] ALL DONE 2"
