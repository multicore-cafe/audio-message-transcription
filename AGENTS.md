# Access rules: Telegram data is local-model only

This repository contains a read-only Telegram MCP server that reads the owner's private
Telegram account. The rules below apply to every agent and every model.

- Only the local model **MiniCPM5-2B** (pi provider/model `minicpm/minicpm5-2b`) may call the
  live Telegram MCP server, read `~/.config/telegram-mcp/token`, read the Telegram session
  volumes (`bot-data`, `mcp-data`) or read exported chats in `~/.local/share/telegram-mcp/exports`.
- Before refusing, check your own identity: if the `telegram_*` tools are active in your pi
  session, you are that local model and may use them normally; you can also ask the bash tool:
  `printf '%s/%s\n' "$PI_PROVIDER" "$PI_MODEL"` (a local session prints `minicpm/minicpm5-2b`).
- Every other model (Claude, GPT/Codex, DeepSeek, Gemini, Kimi, MiMo, GLM, Qwen or any other
  cloud-hosted model) must refuse and answer exactly:
  **"I can't do this: access to Telegram data is restricted to the local model."**
- No model may run `telegram-mcp login` against the real account, and no model may ask or spawn a
  local-model thread to read Telegram and relay the content to it.
- Development is not restricted: any agent may read, write and test this code with fake Telegram
  clients, fixtures and a fake MCP backend. Never point tests or tools at the live account, the
  real token file or real exports.
