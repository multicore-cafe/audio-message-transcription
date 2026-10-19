# pi integration: Telegram MCP tools for the local model only

`index.ts` is a pi extension that exposes the read-only Telegram MCP server as ten `telegram_*`
tools. It is the only client that gets the bearer token, and it works only for the local model
`minicpm/minicpm5-2b`:

- the tools are registered inactive and activated by `session_start` / `model_select` only while
  the current pi model is the local one (a model change deactivates them again);
- every `execute()` re-checks `ctx.model` and throws
  `I can't do this: access to Telegram data is restricted to the local model.` otherwise;
- all tool descriptions start with the same access-policy sentence;
- the token is read lazily, only for the local model, from `TELEGRAM_MCP_TOKEN_FILE`
  (default `~/.config/telegram-mcp/token`); `TELEGRAM_MCP_URL` overrides the default
  `http://127.0.0.1:8790/mcp`.

bb launches pi with `--mode rpc --session ... --extension <bb extension>`, and user extensions in
`~/.pi/agent/extensions/` are still loaded, so this symlink works in bb threads.

## Install

```sh
integrations/pi/install.sh
```

It creates `~/.config/telegram-mcp/token` (mode 0600), the export directory
`~/.local/share/telegram-mcp/exports` (mode 0700) with its `AGENTS.md`, and the symlink
`~/.pi/agent/extensions/telegram-mcp -> <repo>/integrations/pi`. Restart pi (or a bb thread) to
load it.

## Uninstall

```sh
integrations/pi/install.sh --uninstall
```

This removes only the symlink. Delete the token and exports yourself if you want:
`rm -rf ~/.config/telegram-mcp ~/.local/share/telegram-mcp`.

## Tests

```sh
integrations/pi/tests/verify.sh
```

Runs the MCP HTTP app against a fake Telegram account and a fake OpenAI-compatible server:
401 without the token, all eleven tools active for `minicpm/minicpm5-2b`, no `telegram_*` tools for a
cloud model, execution refused for a non-local model, and one real local MiniCPM5-2B call that
uses `telegram_list_dialogs`. No cloud model and no real Telegram account are involved.
