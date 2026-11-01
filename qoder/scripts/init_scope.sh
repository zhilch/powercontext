#!/usr/bin/env bash
# 为「温故」创建一块干净的记忆账本，并写入 3 条示例知识点供评审直接体验。
#
# 用法：
#   bash scripts/init_scope.sh                       # 默认 http://127.0.0.1:8000
#   POWERCONTEXT_URL=http://powercontext:8000 bash scripts/init_scope.sh
#
# 输出最后一行是 scope_id，写进 .env 的 WENGGU_SCOPE_ID 后重启 app 容器。

set -euo pipefail

BASE_URL="${POWERCONTEXT_URL:-http://127.0.0.1:8000}"
SCOPE_KEY="${WENGGU_SCOPE_KEY:-wenggu-main}"

post() {
  curl --fail --silent --show-error --max-time 20 \
    --header 'Content-Type: application/json' --data "$1" "${BASE_URL}$2"
}

echo "1/3 等待 PowerContext 就绪：${BASE_URL}" >&2
for i in $(seq 1 60); do
  if curl --silent --fail --max-time 3 "${BASE_URL}/health/live" >/dev/null 2>&1; then
    break
  fi
  sleep 2
  [ "$i" -eq 60 ] && { echo "PowerContext 未就绪" >&2; exit 1; }
done

echo "2/3 创建 scope（幂等键：${SCOPE_KEY}）" >&2
SCOPE_ID="$(post "{\"title\":\"温故复习教练\",\"summary\":\"知识点掌握度与复习进度\",\"idempotency_key\":\"${SCOPE_KEY}\"}" \
  /v1/scopes | python3 -c 'import json,sys; print(json.load(sys.stdin)["scope_id"])')"
echo "    scope_id=${SCOPE_ID}" >&2

echo "3/3 写入示例知识点" >&2
seed() {
  post "$(python3 - "$SCOPE_ID" "$1" "$2" "$3" <<'PY'
import json, sys, time
scope_id, subject, topic, note = sys.argv[1:5]
now = time.strftime("%Y-%m-%d")
text = "KP|" + "|".join([
    "id=kp_" + format(abs(hash(topic)) % (16 ** 8), "08x"),
    f"subject={subject}",
    f"topic={topic}",
    "score=0", "reviews=0", f"last={now}", f"note={note}", f"created={now}",
    f"ts={int(time.time() * 1000)}",
])
print(json.dumps({"scope_id": scope_id, "kind": "review", "text": text,
                  "reason": f"{subject} · {topic} 的复习记录"}, ensure_ascii=False))
PY
)" /v1/memory/remember > /dev/null
}

seed "高等数学" "泰勒展开的余项形式与适用范围" "总把拉格朗日余项和皮亚诺余项的适用条件搞混"
seed "计算机组成" "Cache 写回与写直达的区别" "答不全写回法的脏位处理"
seed "英语写作" "议论文段落的三段式结构" "论点句和证据句分不清"

echo "初始化完成。WENGGU_SCOPE_ID=${SCOPE_ID}"
