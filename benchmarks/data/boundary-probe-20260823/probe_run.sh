#!/bin/zsh
set -u
cd /Volumes/enkin/project/Agent/backend
OUT=/Volumes/enkin/project/Agent/benchmarks/data/boundary-probe-20260823
for m in gemma-4-26b-a4b-it-4bit gemma-4-26b-a4b-it-8bit; do
  echo "[$(date +%H:%M:%S)] === $m start ==="
  PYTHONUNBUFFERED=1 .venv/bin/python benchmarks/model_eval/run_eval.py --model $m --dims text --spec-mode off --out $OUT/$m
  rc=$?   # 复审修正：原版把 $? 放进含 $(date) 的 echo，永远打 0；当时各 run 以 summary.json 核实
  echo "[$(date +%H:%M:%S)] === $m exit=$rc ==="
done
echo "[$(date +%H:%M:%S)] ALL DONE"
