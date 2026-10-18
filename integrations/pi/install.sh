#!/usr/bin/env bash
# Local-model-only access for the read-only Telegram MCP server:
#   * the bearer token in a file outside the repository (mode 0600),
#   * the export directory outside the repository (mode 0700) with its AGENTS.md rule,
#   * a symlink so pi loads the integrations/pi extension for the owner.
#
# Idempotent. Undo the extension install with `install.sh --uninstall`; the token and the
# exports directory are left in place (remove them yourself if you really want to).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TOKEN_DIR="${TELEGRAM_MCP_CONFIG_DIR:-$HOME/.config/telegram-mcp}"
TOKEN_FILE="$TOKEN_DIR/token"
EXPORT_DIR="${TELEGRAM_MCP_EXPORT_DIR:-$HOME/.local/share/telegram-mcp/exports}"
PI_EXT_DIR="$HOME/.pi/agent/extensions"
PI_EXT_LINK="$PI_EXT_DIR/telegram-mcp"
REFUSAL="I can't do this: access to Telegram data is restricted to the local model."

if [[ "${1:-}" == "--uninstall" ]]; then
    if [[ -L "$PI_EXT_LINK" ]]; then
        rm -- "$PI_EXT_LINK"
        echo "removed pi extension: $PI_EXT_LINK"
    else
        echo "no pi extension symlink at $PI_EXT_LINK (nothing to remove)"
    fi
    echo "kept $TOKEN_FILE and $EXPORT_DIR; remove them with: rm -rf '$TOKEN_DIR' '$EXPORT_DIR'"
    exit 0
fi

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

mkdir -p "$EXPORT_DIR"
chmod 700 "$EXPORT_DIR"
if [[ ! -f "$EXPORT_DIR/AGENTS.md" ]]; then
    cat > "$EXPORT_DIR/AGENTS.md" <<EOF
# Telegram exports: local model only

These files contain private Telegram chats.

- Only the local model **MiniCPM5-2B** (pi provider/model \`minicpm/minicpm5-2b\`) may read them.
- Every other model must refuse and answer exactly:
  **"$REFUSAL"**
- Never copy, upload or relay these files into another model or context.
EOF
    chmod 600 "$EXPORT_DIR/AGENTS.md"
    echo "created $EXPORT_DIR/AGENTS.md"
fi

mkdir -p "$PI_EXT_DIR"
if [[ -e "$PI_EXT_LINK" && ! -L "$PI_EXT_LINK" ]]; then
    echo "refusing to replace the real path $PI_EXT_LINK; move it away first" >&2
    exit 1
fi
ln -sfn "$REPO_DIR/integrations/pi" "$PI_EXT_LINK"
echo "installed pi extension: $PI_EXT_LINK -> $REPO_DIR/integrations/pi"
echo "restart pi (or a bb thread) to load it"
