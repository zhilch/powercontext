#!/usr/bin/env bash
# 初始化《忘川客栈》的 PowerContext 数据：建 scope + 写入开局记忆。
#
# 用法：
#   bash init_powercontext.sh                      # 默认 http://127.0.0.1:8000
#   POWERCONTEXT_URL=http://powercontext:8000 bash init_powercontext.sh
#
# 输出最后一行是 scope_id，把它填进 Dify 工作流的环境变量 WANGCHUAN_SCOPE_ID。

set -euo pipefail

BASE_URL="${POWERCONTEXT_URL:-http://127.0.0.1:8000}"
SCOPE_KEY="${WANGCHUAN_SCOPE_KEY:-wangchuan-inn-main}"

post() {
  local path="$1"
  local body="$2"
  curl --fail --silent --show-error \
    --max-time 20 \
    --header 'Content-Type: application/json' \
    --data "$body" \
    "${BASE_URL}${path}"
}

echo "1/3 等待 PowerContext 就绪：${BASE_URL}" >&2
for i in $(seq 1 60); do
  if curl --silent --fail --max-time 3 "${BASE_URL}/health/live" >/dev/null 2>&1; then
    break
  fi
  sleep 2
  if [ "$i" -eq 60 ]; then
    echo "PowerContext 未就绪，请先启动 powercontext 容器" >&2
    exit 1
  fi
done

echo "2/3 创建 scope（幂等键：${SCOPE_KEY}）" >&2
SCOPE_JSON="$(post /v1/scopes "$(cat <<JSON
{"title":"忘川客栈","summary":"互动剧情游戏：客栈掌柜的记忆账本","idempotency_key":"${SCOPE_KEY}"}
JSON
)")"
SCOPE_ID="$(printf '%s' "$SCOPE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["scope_id"])')"
echo "    scope_id=${SCOPE_ID}" >&2

echo "3/3 写入开局记忆" >&2
seed() {
  local kind="$1" text="$2" reason="$3"
  post /v1/memory/remember "$(python3 - "$SCOPE_ID" "$kind" "$text" "$reason" <<'PY'
import json, sys
scope_id, kind, text, reason = sys.argv[1:5]
print(json.dumps({"scope_id": scope_id, "kind": kind, "text": text, "reason": reason}, ensure_ascii=False))
PY
)" > /dev/null
}

seed "setting" "客栈规矩：客人喝下忘川水，次日醒来会忘记一切；只有掌柜阿箬的《记忆账本》能留下东西。" "开局设定"
seed "npc" "掌柜阿箬：说话克制、带一点江湖气，习惯用酒和旧事试探客人。" "角色设定"
seed "rule" "账本上没写过的事，阿箬一律不知道，只会露出陌生的神色，不会编造客人说过的话。" "玩法规则"

echo "初始化完成。WANGCHUAN_SCOPE_ID=${SCOPE_ID}"
