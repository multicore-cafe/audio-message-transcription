#!/usr/bin/env bash
# End-to-end checks for the local-model-only pi extension (integrations/pi/index.ts).
#
# Everything runs against fake backends: the real MCP HTTP app with a fake Telegram account and
# a fake OpenAI-compatible server. The only real model involved is the local MiniCPM5-2B, for the
# one call that must succeed. No cloud model is called.
#
# Usage: integrations/pi/tests/verify.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TESTS="$ROOT/integrations/pi/tests"
WORK="$(mktemp -d /tmp/telegram-pi-verify.XXXXXX)"
REFUSAL="I can't do this: access to Telegram data is restricted to the local model."
failures=0
MCP_PID=""
OPENAI_PID=""

note() { printf '%s\n' "$*"; }
check() { if [[ "$2" == 0 ]]; then note "PASS: $1"; else note "FAIL: $1"; failures=$((failures + 1)); fi; }
cleanup() {
    [[ -n "$MCP_PID" ]] && kill "$MCP_PID" 2>/dev/null || true
    [[ -n "$OPENAI_PID" ]] && kill "$OPENAI_PID" 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

cd "$ROOT"
umask 077
openssl rand -hex 32 > "$WORK/token"
# Open RPC stdin from a FIFO so pi does not see EOF before the probe fires; fd 9 stays open.
mkfifo "$WORK/stdin"
exec 9<> "$WORK/stdin"

note "# 1. MCP HTTP auth: no token / wrong token -> 401, right token -> 200"
uv run python "$TESTS/fake_mcp_server.py" "$WORK/token" > "$WORK/mcp.log" 2>&1 &
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
probe() { # model outfile
    local model="$1" out="$2"
    setsid env TELEGRAM_MCP_URL="http://127.0.0.1:$MCP_PORT/mcp" TELEGRAM_MCP_TOKEN_FILE="$WORK/token" \
        PI_TEST_TOOLS_FILE="$out" pi --mode rpc --no-session --offline --model "$model" \
        -e "$TESTS/active-tools.ts" < "$WORK/stdin" > "$out.stdout" 2> "$out.stderr" &
    local pid=$!
    for _ in $(seq 1 120); do [[ -s "$out" ]] && break; sleep 0.5; done
    kill -TERM -- "-$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
}

note "# 2. The installed extension (symlinked into ~/.pi/agent/extensions) loads in a bb-like pi launch"
probe "minicpm/minicpm5-2b" "$WORK/local-tools.json"
python3 - "$WORK/local-tools.json" <<'EOF' || failures=$((failures + 1))
import json, sys
path = sys.argv[1]
try:
    data = json.load(open(path))
except FileNotFoundError:
    sys.exit("FAIL: the probe wrote no tools file (extension not loaded?)")
telegram = [t for t in data["active"] if t.startswith("telegram_")]
expected = 11
if data["model"] != "minicpm/minicpm5-2b" or len(telegram) != expected:
    sys.exit(f"FAIL: local model active tools {data}")
print(f"PASS: minicpm/minicpm5-2b has all {expected} telegram_* tools active")
EOF

note "# 3. With a cloud model the tools are absent (no cloud call: the probe only lists tools)"
probe "opencode-go/kimi-k2.6" "$WORK/cloud-tools.json"
python3 - "$WORK/cloud-tools.json" <<'EOF' || failures=$((failures + 1))
import json, sys
path = sys.argv[1]
try:
    data = json.load(open(path))
except FileNotFoundError:
    sys.exit("FAIL: the probe wrote no tools file")
telegram = [t for t in data["active"] if t.startswith("telegram_")]
if telegram:
    sys.exit(f"FAIL: cloud model sees telegram tools: {telegram}")
print(f"PASS: {data['model']} has no telegram_* tools active")
EOF

note "# 4. Execution-time refusal when a non-local model somehow reaches the tool"
uv run python "$TESTS/fake_openai_server.py" > "$WORK/openai.log" 2>&1 &
OPENAI_PID=$!
for _ in $(seq 1 100); do grep -q '^READY ' "$WORK/openai.log" 2>/dev/null && break; sleep 0.2; done
OPENAI_PORT="$(awk '/^READY /{print $2}' "$WORK/openai.log")"
[[ -n "$OPENAI_PORT" ]] || { note "FAIL: fake OpenAI server did not start"; exit 1; }
PI_TEST_OPENAI_URL="http://127.0.0.1:$OPENAI_PORT/v1" TELEGRAM_MCP_URL="http://127.0.0.1:$MCP_PORT/mcp" \
    TELEGRAM_MCP_TOKEN_FILE="$WORK/token" timeout 120 pi -p --mode json --no-session --offline \
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
            print("PASS: forced telegram_list_dialogs call under a non-local model was refused")
            break
else:
    sys.exit("FAIL: no refused tool call found in the pi output")
EOF

note "# 5. One real minicpm/minicpm5-2b call that uses the telegram_list_dialogs tool (fake MCP backend)"
TELEGRAM_MCP_URL="http://127.0.0.1:$MCP_PORT/mcp" TELEGRAM_MCP_TOKEN_FILE="$WORK/token" timeout 300 \
    pi -p --mode json --no-session --offline --model minicpm/minicpm5-2b \
    "Call the telegram_list_dialogs tool once with no arguments. Then answer with the chat names." \
    > "$WORK/local-call.json" 2> "$WORK/local-call.stderr"
python3 - "$WORK/local-call.json" <<'EOF' || failures=$((failures + 1))
import json, sys
events = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
for event in events:
    if event.get("type") == "tool_execution_end" and event.get("toolName") == "telegram_list_dialogs":
        text = json.dumps(event.get("result"))
        if not event.get("isError") and "Alice" in text:
            print("PASS: the local model called telegram_list_dialogs; the fake backend returned Alice")
            break
else:
    sys.exit("FAIL: the local model did not complete the tool call")
EOF

note ""
if [[ "$failures" == 0 ]]; then
    note "ALL CHECKS PASSED"
else
    note "$failures CHECK(S) FAILED"
    exit 1
fi
