#!/usr/bin/env bash
# 三体协奏派发示例
# 用法: ./scripts/dispatch.sh <agent> <prompt>
set -e
BRIDGE="${BRIDGE:-http://127.0.0.1:8770}"
AGENT="${1:-opencode}"
PROMPT="${2:-输出 1+1 等于几}"

echo "→ 派发到 $AGENT: $PROMPT"
RESP=$(curl -s -X POST "$BRIDGE/dispatch" \
  -H "Content-Type: application/json" \
  -d "{\"target_agent\":\"$AGENT\",\"prompt\":\"$PROMPT\"}")
TASK_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('task_id',''))")
echo "→ task_id: $TASK_ID"

for i in $(seq 1 20); do
  sleep 3
  STATUS=$(curl -s "$BRIDGE/tasks/$TASK_ID" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))")
  echo "[$((i*3))s] $STATUS"
  [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ] && break
done

curl -s "$BRIDGE/tasks/$TASK_ID" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(f'→ 结果: status={d.get(\"status\")} rc={d.get(\"returncode\")} elapsed={d.get(\"elapsed\")}s')
print(d.get('output','')[:500])
"
