#!/usr/bin/env bash
# Access setup for the read-only Telegram MCP server:
#   * the allowed-models file outside the repository (which pi provider/model pairs may use it),
#   * the bearer token in a file outside the repository (mode 0600),
#   * the export directory outside the repository (mode 0700) with its AGENTS.md rule,
#   * a symlink so pi loads the integrations/pi extension for the owner.
#
# Idempotent. The allow-list is created only when it does not exist yet and never overwritten,
# with entries from repeatable `--allow provider/model` (or `--allow provider/*`) arguments or an
# interactive prompt. Without either, no list is created and the system stays fail-closed.
# Undo the extension install with `install.sh --uninstall`; the allow-list, token and exports
# directory are left in place (remove them yourself if you really want to).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOKEN_DIR="${TELEGRAM_MCP_CONFIG_DIR:-$HOME/.config/telegram-mcp}"
TOKEN_FILE="$TOKEN_DIR/token"
ALLOWED_FILE="${TELEGRAM_MCP_ALLOWED_MODELS_FILE:-$TOKEN_DIR/allowed-models}"
EXPORT_DIR="${TELEGRAM_MCP_EXPORT_DIR:-$HOME/.local/share/telegram-mcp/exports}"
PI_EXT_DIR="$HOME/.pi/agent/extensions"
PI_EXT_LINK="$PI_EXT_DIR/telegram-mcp"
REFUSAL="I can't do this: access to Telegram data is restricted to the models allowed by the owner."
ALLOW_ENTRIES=()

usage() {
    cat <<EOF
Usage: install.sh [--allow provider/model]... [--uninstall]

  --allow provider/model   Add an allowed entry (repeatable; use provider/* for a whole provider).
                           Entries are written only when the allow-list file does not exist yet.
  --uninstall              Remove the pi extension symlink (keeps the allow-list, token, exports).
EOF
}

UNINSTALL=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --uninstall)
            UNINSTALL=1
            shift
            ;;
        --allow)
            if [[ $# -lt 2 ]]; then
                echo "--allow needs a provider/model argument" >&2
                exit 2
            fi
            ALLOW_ENTRIES+=("$2")
            shift 2
            ;;
        --allow=*)
            ALLOW_ENTRIES+=("${1#--allow=}")
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ "$UNINSTALL" == 1 ]]; then
    if [[ -L "$PI_EXT_LINK" ]]; then
        rm -- "$PI_EXT_LINK"
        echo "removed pi extension: $PI_EXT_LINK"
    else
        echo "no pi extension symlink at $PI_EXT_LINK (nothing to remove)"
    fi
    echo "kept $ALLOWED_FILE, $TOKEN_FILE and $EXPORT_DIR; remove them with: rm -rf '$TOKEN_DIR' '$EXPORT_DIR'"
    exit 0
fi

# A valid entry is `provider/model-id` or `provider/*`, split on the first `/`; model ids may
# contain `/`. Blank lines and `#` comments are ignored.
valid_entry() {
    local entry="$1" provider model
    [[ "$entry" == */* ]] || return 1
    [[ "$entry" != *[[:space:]]* ]] || return 1
    provider="${entry%%/*}"
    model="${entry#*/}"
    [[ -n "$provider" && -n "$model" && "$provider" != "*" ]]
}

umask 077

mkdir -p "$TOKEN_DIR"
if [[ -s "$TOKEN_FILE" ]]; then
    echo "keeping existing token file $TOKEN_FILE"
else
    # Never printed: written straight to the file with the restricted umask.
    openssl rand -hex 32 > "$TOKEN_FILE"
    echo "generated token file $TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"

# The allow-list is not secret (mode 0644) and must never be overwritten by a re-run.
if [[ -e "$ALLOWED_FILE" ]]; then
    echo "keeping existing allowed-models file $ALLOWED_FILE"
else
    entries=("${ALLOW_ENTRIES[@]}")
    if [[ ${#entries[@]} -eq 0 && -t 0 ]]; then
        read -r -p "Allowed provider/model entries, space-separated (e.g. local/my-model someprovider/*): " reply || true
        # shellcheck disable=SC2206
        entries=($reply)
    fi
    if [[ ${#entries[@]} -gt 0 ]]; then
        for entry in "${entries[@]}"; do
            if ! valid_entry "$entry"; then
                echo "refusing invalid allow-list entry: $entry (expected provider/model or provider/*)" >&2
                exit 2
            fi
        done
        mkdir -p "$(dirname "$ALLOWED_FILE")"
        printf '%s\n' "${entries[@]}" > "$ALLOWED_FILE"
        chmod 644 "$ALLOWED_FILE"
        echo "wrote allowed-models file $ALLOWED_FILE"
    else
        echo "no allowed-models file created at $ALLOWED_FILE"
        echo "nothing may use the Telegram MCP until you create it, e.g.:"
        echo "  integrations/pi/install.sh --allow <provider/model>   # repeat --allow for more entries"
    fi
fi

mkdir -p "$EXPORT_DIR"
chmod 700 "$EXPORT_DIR"
exports_agents_md() {
    cat <<EOF
# Telegram exports: allow-list only

These files contain private Telegram chats.

- Only the models listed in the allowed-models file (\`$ALLOWED_FILE\`) may read them.
- If that file is missing or your provider/model is not listed there, refuse and answer exactly:
  **"$REFUSAL"**
- Never copy, upload or relay these files into another model or context.
EOF
}
# Written only when the content is missing or outdated, so re-running stays idempotent
# (and never rewrites the token file).
if [[ ! -f "$EXPORT_DIR/AGENTS.md" ]] || [[ "$(exports_agents_md)" != "$(cat "$EXPORT_DIR/AGENTS.md")" ]]; then
    exports_agents_md > "$EXPORT_DIR/AGENTS.md"
    chmod 600 "$EXPORT_DIR/AGENTS.md"
    echo "wrote $EXPORT_DIR/AGENTS.md"
fi

mkdir -p "$PI_EXT_DIR"
if [[ -e "$PI_EXT_LINK" && ! -L "$PI_EXT_LINK" ]]; then
    echo "refusing to replace the real path $PI_EXT_LINK; move it away first" >&2
    exit 1
fi
ln -sfn "$REPO_DIR/integrations/pi" "$PI_EXT_LINK"
echo "installed pi extension: $PI_EXT_LINK -> $REPO_DIR/integrations/pi"
echo "restart pi (or a bb thread) to load it"
