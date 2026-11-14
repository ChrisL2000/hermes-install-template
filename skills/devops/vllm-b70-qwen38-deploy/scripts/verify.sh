#!/usr/bin/env bash
# Prove the deployed server works: /health, /v1/models, one real chat completion.
#   verify.sh [base_url]   (default from .env HOST_PORT, else http://localhost:8081)
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$here/.env" ]] && { set -a; # shellcheck disable=SC1091
  source "$here/.env"; set +a; }
base="${1:-http://localhost:${HOST_PORT:-8081}}"
model="${SERVED_MODEL_NAME:-Qwen3.8-27B}"

code="$(curl -s -o /dev/null -w '%{http_code}' "$base/health")"
[[ "$code" == 200 ]] || { echo "FAIL /health -> $code" >&2; exit 1; }
echo "ok   /health 200"

models="$(curl -s "$base/v1/models")"
echo "$models" | grep -q "\"id\":\"$model\"\|\"id\": \"$model\"" || { echo "FAIL model $model not in /v1/models: $models" >&2; exit 1; }
echo "ok   /v1/models lists $model"

resp="$(curl -s "$base/v1/chat/completions" -H 'Content-Type: application/json' -d "{
  \"model\": \"$model\", \"max_tokens\": 64, \"temperature\": 0, \"reasoning_effort\": \"none\",
  \"messages\": [{\"role\": \"user\", \"content\": \"Reply with exactly: B70 online\"}]}")"
python3 - "$resp" <<'PY'
import json, sys
r = json.loads(sys.argv[1])
txt = r["choices"][0]["message"]["content"].strip()
u = r.get("usage", {})
assert u.get("completion_tokens", 0) > 0, r
print(f"ok   completion: {txt!r}  prompt={u.get('prompt_tokens')} completion={u.get('completion_tokens')}")
PY

if command -v docker >/dev/null && docker inspect "${CONTAINER_NAME:-vllm-b70-qwen38-gptq}" >/dev/null 2>&1; then
  docker logs "${CONTAINER_NAME:-vllm-b70-qwen38-gptq}" 2>&1 | grep -E 'XPU KV cache size' | tail -1 | sed 's/^/info /'
fi
echo "PASS"
