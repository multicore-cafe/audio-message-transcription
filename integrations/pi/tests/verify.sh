#!/usr/bin/env bash
# End-to-end checks for the allowed-models pi extension (integrations/pi/index.ts).
#
# Everything runs against fake backends: the real MCP HTTP app with a fake Telegram account and
# a fake OpenAI-compatible server. Provider/model gate probes use fake providers registered by a
# test extension (fake-models.ts), so no real cloud or local model is called; only the final
# step makes a real model call and runs only when VERIFY_REAL_MODEL is set.
#
# Usage: integrations/pi/tests/verify.sh
#        VERIFY_REAL_MODEL=<provider>/<model> integrations/pi/tests/verify.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TESTS="$ROOT/integrations/pi/tests"
WORK="$(mktemp -d /tmp/telegram-pi-verify.XXXXXX)"
REFUSAL="I can't do this: access to Telegram data is restricted to the models allowed by the owner."
failures=0
MCP_PID=""
OPENAI_PIDS=()
OPENAI_PORT=""

note() { printf '%s\n' "$*"; }
check() { if [[ "$2" == 0 ]]; then note "PASS: $1"; else note "FAIL: $1"; failures=$((failures + 1)); fi; }
cleanup() {
    [[ -n "$MCP_PID" ]] && kill "$MCP_PID" 2>/dev/null || true
    for pid in "${OPENAI_PIDS[@]:-}"; do
        [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
    done
    rm -rf "$WORK"
}
trap cleanup EXIT

# start_openai <logfile> -> sets OPENAI_PORT. A fresh server answers its first request with a
# tool call, so each refusal check gets its own instance.
start_openai() {
    uv run python "$TESTS/fake_openai_server.py" > "$1" 2>&1 &
    OPENAI_PIDS+=("$!")
    for _ in $(seq 1 100); do grep -q '^READY ' "$1" 2>/dev/null && break; sleep 0.2; done
    OPENAI_PORT="$(awk '/^READY /{print $2}' "$1")"
    [[ -n "$OPENAI_PORT" ]] || { note "FAIL: fake OpenAI server did not start"; cat "$1"; exit 1; }
}

cd "$ROOT"
umask 077
openssl rand -hex 32 > "$WORK/token"
# Neutral test allow-list: one exact provider/model, one exact model id containing `/`, and one
# provider wildcard. Personal model choices never appear in this repository.
cat > "$WORK/allowed-models" <<'EOF'
# one entry per line: provider/model-id or provider/*
oneprovider/my-model
someprovider/team/turbo
otherprovider/*
EOF
MISSING_ALLOWED="$WORK/does-not-exist/allowed-models"
# Open RPC stdin from a FIFO so pi does not see EOF before the probe fires; fd 9 stays open.
mkfifo "$WORK/stdin"
exec 9<> "$WORK/stdin"

note "# 1. MCP HTTP auth: no token / wrong token -> 401, right token -> 200"
uv run python "$TESTS/fake_mcp_server.py" "$WORK/token" "$WORK/allowed-models" > "$WORK/mcp.log" 2>&1 &
MCP_PID=$!
for _ in $(seq 1 150); do grep -q '^READY ' "$WORK/mcp.log" 2>/dev/null && break; sleep 0.2; done
MCP_PORT="$(awk '/^READY /{print $2}' "$WORK/mcp.log")"
[[ -n "$MCP_PORT" ]] || { note "FAIL: fake MCP server did not start"; cat "$WORK/mcp.log"; exit 1; }
curl_headers=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream')
body='{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
code_no_token="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$MCP_PORT/mcp" "${curl_headers[@]}" -d "$body")"
code_wrong="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$MCP_PORT/mcp" "${curl_headers[@]}" \
    -H 'Authorization: Bearer wrong' -d "$body")"
code_right="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$MCP_PORT/mcp" "${curl_headers[@]}" \
    -H "Authorization: Bearer $(<"$WORK/token")" -d "$body")"
if [[ "$code_no_token" == 401 ]]; then check "request without a token gets 401 (got $code_no_token)" 0; \
    else check "request without a token gets 401 (got $code_no_token)" 1; fi
if [[ "$code_wrong" == 401 ]]; then check "request with a wrong token gets 401 (got $code_wrong)" 0; \
    else check "request with a wrong token gets 401 (got $code_wrong)" 1; fi
if [[ "$code_right" == 200 ]]; then check "request with the right token gets 200 (got $code_right)" 0; \
    else check "request with the right token gets 200 (got $code_right)" 1; fi

# Runs `pi --mode rpc` with an open stdin, waits for the active-tools probe file, then kills it.
# probe <model> <outfile> [allowed-models-file]
probe() {
    local model="$1" out="$2" allow="${3:-$WORK/allowed-models}"
    setsid env TELEGRAM_MCP_URL="http://127.0.0.1:$MCP_PORT/mcp" TELEGRAM_MCP_TOKEN_FILE="$WORK/token" \
        TELEGRAM_MCP_ALLOWED_MODELS_FILE="$allow" PI_TEST_TOOLS_FILE="$out" \
        pi --mode rpc --no-session --offline --model "$model" \
        -e "$TESTS/fake-models.ts" -e "$TESTS/active-tools.ts" < "$WORK/stdin" > "$out.stdout" 2> "$out.stderr" &
    local pid=$!
    for _ in $(seq 1 120); do [[ -s "$out" ]] && break; sleep 0.5; done
    kill -TERM -- "-$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
}

# expect_tools <model> <outfile> <count> <description> [allowed-models-file]
expect_tools() {
    local model="$1" out="$2" count="$3" label="$4" allow="${5:-$WORK/allowed-models}"
    probe "$model" "$out" "$allow"
    python3 - "$out" "$model" "$count" "$label" <<'EOF' || failures=$((failures + 1))
import json, sys
path, model, expected, label = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
try:
    data = json.load(open(path))
except FileNotFoundError:
    sys.exit(f"FAIL: the probe for {model} wrote no tools file (extension not loaded?)")
telegram = [t for t in data["active"] if t.startswith("telegram_")]
if data["model"] != model:
    sys.exit(f"FAIL: expected model {model}, got {data['model']}")
if len(telegram) != expected:
    sys.exit(f"FAIL: {label}: expected {expected} telegram_* tools, got {len(telegram)}: {telegram}")
print(f"PASS: {label}")
EOF
}

note "# 2. An allow-listed exact model has all eleven telegram_* tools active"
expect_tools "oneprovider/my-model" "$WORK/exact-tools.json" 11 \
    "oneprovider/my-model (allow-listed exact) has all 11 telegram_* tools"

note "# 3. The model id may itself contain / (split on the first /)"
expect_tools "someprovider/team/turbo" "$WORK/slash-tools.json" 11 \
    "someprovider/team/turbo (allow-listed exact with / in the id) has all 11 telegram_* tools"

note "# 4. A provider/* entry allows every model of that provider"
expect_tools "otherprovider/anything" "$WORK/wildcard-tools.json" 11 \
    "otherprovider/anything (allow-listed provider wildcard) has all 11 telegram_* tools"

note "# 5. A non-matching model of an allow-listed provider sees nothing"
expect_tools "someprovider/team/other" "$WORK/mismatch-tools.json" 0 \
    "someprovider/team/other (exact entry does not match) has no telegram_* tools"

note "# 6. A model of an unlisted provider sees nothing"
expect_tools "cloudprovider/kimi" "$WORK/cloud-tools.json" 0 \
    "cloudprovider/kimi (unlisted provider) has no telegram_* tools"

note "# 7. Fail closed: a missing allow-list file activates no tools even for a listed model"
expect_tools "someprovider/team/turbo" "$WORK/no-file-tools.json" 0 \
    "someprovider/team/turbo with a missing allowed-models file has no telegram_* tools" "$MISSING_ALLOWED"

note "# 8. Execution-time refusal when a non-allowed model somehow reaches the tool"
start_openai "$WORK/openai.log"
PI_TEST_OPENAI_URL="http://127.0.0.1:$OPENAI_PORT/v1" TELEGRAM_MCP_URL="http://127.0.0.1:$MCP_PORT/mcp" \
    TELEGRAM_MCP_TOKEN_FILE="$WORK/token" TELEGRAM_MCP_ALLOWED_MODELS_FILE="$WORK/allowed-models" \
    timeout 120 pi -p --mode json --no-session --offline \
    --model local-check/cloud-proxy -e "$TESTS/force-tools-cloud.ts" "List my Telegram chats." \
    > "$WORK/refusal.json" 2> "$WORK/refusal.stderr"
python3 - "$WORK/refusal.json" "$REFUSAL" <<'EOF' || failures=$((failures + 1))
import json, sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
refusal = sys.argv[2]
for event in events:
    if event.get("type") == "tool_execution_end" and event.get("toolName") == "telegram_list_dialogs":
        text = json.dumps(event.get("result"))
        if event.get("isError") and refusal in text:
            print("PASS: forced telegram_list_dialogs call under a non-allowed model was refused")
            break
else:
    sys.exit("FAIL: no refused tool call found in the pi output")
EOF

note "# 9. Fail closed: execution with a missing allow-list file refuses and names the file"
start_openai "$WORK/openai-no-file.log"
PI_TEST_OPENAI_URL="http://127.0.0.1:$OPENAI_PORT/v1" TELEGRAM_MCP_URL="http://127.0.0.1:$MCP_PORT/mcp" \
    TELEGRAM_MCP_TOKEN_FILE="$WORK/token" TELEGRAM_MCP_ALLOWED_MODELS_FILE="$MISSING_ALLOWED" \
    timeout 120 pi -p --mode json --no-session --offline \
    --model local-check/cloud-proxy -e "$TESTS/force-tools-cloud.ts" "List my Telegram chats." \
    > "$WORK/no-file-refusal.json" 2> "$WORK/no-file-refusal.stderr"
python3 - "$WORK/no-file-refusal.json" "$MISSING_ALLOWED" <<'EOF' || failures=$((failures + 1))
import json, sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
path = sys.argv[2]
for event in events:
    if event.get("type") == "tool_execution_end" and event.get("toolName") == "telegram_list_dialogs":
        text = json.dumps(event.get("result"))
        if event.get("isError") and path in text and "allowed-models" in text:
            print("PASS: a missing allow-list file refuses execution and names the file")
            break
else:
    sys.exit("FAIL: no fail-closed tool refusal found in the pi output")
EOF

note "# 10. Real-model call (set VERIFY_REAL_MODEL=provider/model to run it)"
if [[ -n "${VERIFY_REAL_MODEL:-}" ]]; then
    printf '%s\n' "$VERIFY_REAL_MODEL" > "$WORK/real-allow"
    TELEGRAM_MCP_URL="http://127.0.0.1:$MCP_PORT/mcp" TELEGRAM_MCP_TOKEN_FILE="$WORK/token" \
        TELEGRAM_MCP_ALLOWED_MODELS_FILE="$WORK/real-allow" timeout 300 \
        pi -p --mode json --no-session --offline --model "$VERIFY_REAL_MODEL" \
        "Call the telegram_list_dialogs tool once with no arguments. Then answer with the chat names." \
        > "$WORK/local-call.json" 2> "$WORK/local-call.stderr"
    python3 - "$WORK/local-call.json" <<'EOF' || failures=$((failures + 1))
import json, sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
for event in events:
    if event.get("type") == "tool_execution_end" and event.get("toolName") == "telegram_list_dialogs":
        text = json.dumps(event.get("result"))
        if not event.get("isError") and "Alice" in text:
            print("PASS: the allow-listed real model called telegram_list_dialogs; the fake backend returned Alice")
            break
else:
    sys.exit("FAIL: the allow-listed real model did not complete the tool call")
EOF
else
    note "SKIP: set VERIFY_REAL_MODEL=<provider>/<model> to run one real allow-listed model call"
fi

note ""
if [[ "$failures" == 0 ]]; then
    note "ALL CHECKS PASSED"
else
    note "$failures CHECK(S) FAILED"
    exit 1
fi
