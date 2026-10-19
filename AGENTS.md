# Access rules: Telegram data is allowed-models only

This repository contains a read-only Telegram MCP server that reads the owner's private
Telegram account. The rules below apply to every agent and every model.

- Allowed models: the local model **MiniCPM5-2B** (pi provider/model `minicpm/minicpm5-2b`) and
  any **Privatemode** model (pi provider `privatemode`, any model id). Only they may call the
  live Telegram MCP server, read `~/.config/telegram-mcp/token`, read the Telegram session
  volumes (`bot-data`, `mcp-data`) or read exported chats in `~/.local/share/telegram-mcp/exports`.
- Before refusing, check your own identity: if the `telegram_*` tools are active in your pi
  session, you are an allowed model and may use them normally; you can also ask the bash tool:
  `printf '%s/%s\n' "$PI_PROVIDER" "$PI_MODEL"` (an allowed session prints `minicpm/minicpm5-2b`
  or `privatemode/<model id>`, e.g. `privatemode/glm-5.3-flash`).
- Every other model (Claude, GPT/Codex, DeepSeek, Gemini, Kimi, MiMo, GLM or GPT via any
  non-privatemode provider, Qwen or any other cloud-hosted model) must refuse and answer exactly:
  **"I can't do this: access to Telegram data is restricted to the local model and Privatemode models."**
- The `local` pi provider (`local/bonsai`, `local/k2h4b`, `local/minicpm2b`) is not allowed.
- No model may run `telegram-mcp login` against the real account, and no model may ask or spawn an
  allowed-model thread to read Telegram and relay the content to it.
- Development is not restricted: any agent may read, write and test this code with fake Telegram
  clients, fixtures and a fake MCP backend. Never point tests or tools at the live account, the
  real token file or real exports.
