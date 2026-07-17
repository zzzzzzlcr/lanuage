#!/bin/bash
# 自然语言 → JSON → 验证
# Usage: echo "页面: xxx 类型: xxx 操作: ..." | ./run.sh
# Or: ./run.sh description.txt

DESC=${1:-/dev/stdin}
python3 src/json_pipeline.py --description "$DESC" \
  --ws-url "${WS_URL}" \
  --navigate "${NAVIGATE_URL}" \
  --profile "${PROFILE:-{\"task_id\":\"cli\"}}"
