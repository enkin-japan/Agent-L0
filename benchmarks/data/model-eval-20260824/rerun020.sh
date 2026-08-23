#!/bin/bash
# tc-real-020 修订版（剔除 run_shell_command 干扰项）七档单题补跑
set -u
BACKEND=/Volumes/enkin/project/Agent/backend
DIR=/Volumes/enkin/project/Agent/benchmarks/data/model-eval-20260824
cd "$BACKEND"
export PYTHONUNBUFFERED=1
for m in Ornith-1.5-35B-A3B-MLX-4bit Ornith-1.5-35B-A3B-MLX-6bit Ornith-1.5-35B-A3B-MLX-8bit \
         gemma-4-26b-a4b-it-4bit gemma-4-26b-a4b-it-8bit Qwen3.8-27B-4bit Qwen3.8-27B-8bit; do
  echo "== rerun020 $m"
  .venv/bin/python benchmarks/model_eval/run_eval.py --model "$m" --dims tool_call \
      --ids tc-real-020 --out "$DIR/rerun020/$m" 2>&1 | grep -E "^✅|^❌|^== summary"
done
echo "== RERUN020 DONE"
