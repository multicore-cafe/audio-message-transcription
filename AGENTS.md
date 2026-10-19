# Access rules: Telegram data is allow-list only

This repository contains a Telegram MCP server that reads the owner's private Telegram account.
It is read-only except saving into the owner's own Saved Messages (the opt-in `send_to_saved` /
`forward_to_saved` tools, `MCP_SAVED_MESSAGES_WRITE=1`). The rules below apply to every agent and
every model.

- Only the models listed in the allowed-models file may touch live Telegram data: call the MCP
  server, read `~/.config/telegram-mcp/token`, read the Telegram session volumes (`bot-data`,
  `mcp-data`), read exported chats in `~/.local/share/telegram-mcp/exports`, or run
  `telegram-mcp login`.
- The allowed-models file defaults to `~/.config/telegram-mcp/allowed-models`; the pi extension
  honours `TELEGRAM_MCP_ALLOWED_MODELS_FILE` and the MCP container uses
  `MCP_ALLOWED_MODELS_FILE`. It is not secret and you may read it to check yourself: one entry
  per line, `provider/model-id` (exact) or `provider/*` (any model of that provider); blank lines
  and `#` comments are ignored.
- Check your own identity before refusing: if the `telegram_*` tools are active in your pi
  session you are an allowed model and may use them normally; you can also compare the file with
  the bash tool: `printf '%s/%s\n' "$PI_PROVIDER" "$PI_MODEL"`.
- If the file is missing, unreadable, empty or invalid, or your provider/model is not listed,
  you must refuse and answer exactly:
  **"I can't do this: access to Telegram data is restricted to the models allowed by the owner."**
- No model may run `telegram-mcp login` against the real account, and no model may ask or spawn an
  allowed-model thread to read Telegram and relay the content to it.
- Development is not restricted: any agent may read, write and test this code with fake Telegram
  clients, fixtures and a fake MCP backend. Never point tests or tools at the live account, the
  real token file or real exports.
