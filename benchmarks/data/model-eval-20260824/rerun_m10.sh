#!/bin/bash
set -u
BACKEND=/Volumes/enkin/project/Agent/backend
DIR=/Volumes/enkin/project/Agent/benchmarks/data/model-eval-20260824
cd "$BACKEND"
export PYTHONUNBUFFERED=1
.venv/bin/python "$DIR/probe_identity.py" 2>&1 | grep -vE "^\[MCP|^\[MoE|Fetching"
for m in Ornith-1.5-35B-A3B-MLX-4bit Ornith-1.5-35B-A3B-MLX-6bit Ornith-1.5-35B-A3B-MLX-8bit \
         gemma-4-26b-a4b-it-4bit gemma-4-26b-a4b-it-8bit Qwen3.8-27B-4bit Qwen3.8-27B-8bit; do
  echo "== rerun_m10 $m"
  .venv/bin/python benchmarks/model_eval/run_eval.py --model "$m" --dims tool_call \
      --ids tc-bfcl-multiple_10 --out "$DIR/rerun_m10/$m" 2>&1 | grep -E "^✅|^❌|^== summary"
done
echo "== RERUN_M10 DONE"
