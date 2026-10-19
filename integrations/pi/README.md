# pi integration: Telegram MCP tools for allow-listed models only

`index.ts` is a pi extension that exposes the read-only Telegram MCP server as eleven `telegram_*`
tools. It is the only client that gets the bearer token, and it works only for the models listed
in the allowed-models file (default `~/.config/telegram-mcp/allowed-models`, override with
`TELEGRAM_MCP_ALLOWED_MODELS_FILE`):

- the tools are registered inactive and activated by `session_start` / `model_select` only while
  the current pi provider/model is listed (a model change deactivates them again);
- every `execute()` re-reads the file, re-checks `ctx.model` and throws
  `I can't do this: access to Telegram data is restricted to the models allowed by the owner.`
  otherwise;
- all tool descriptions start with the same generated access-policy text naming the list;
- the token is read lazily, only for an allowed model, from `TELEGRAM_MCP_TOKEN_FILE`
  (default `~/.config/telegram-mcp/token`); `TELEGRAM_MCP_URL` overrides the default
  `http://127.0.0.1:8790/mcp`.

Fail closed: a missing, unreadable, empty or invalid allow-list file allows no model; execution
is refused with a message naming the file and how to create it.

The file format is one entry per line: `provider/model-id` (exact) or `provider/*` (any model of
that provider). The entry is split on the first `/`, so model ids may contain `/`
(`someprovider/team/turbo`). Blank lines and `#` comments are ignored.

bb launches pi with `--mode rpc --session ... --extension <bb extension>`, and user extensions in
`~/.pi/agent/extensions/` are still loaded, so this symlink works in bb threads.

## Install

```sh
integrations/pi/install.sh --allow <provider/model>   # repeat --allow for more entries
```

It creates `~/.config/telegram-mcp/allowed-models` (mode 0644, only if missing), the token
`~/.config/telegram-mcp/token` (mode 0600), the export directory
`~/.local/share/telegram-mcp/exports` (mode 0700) with its `AGENTS.md`, and the symlink
`~/.pi/agent/extensions/telegram-mcp -> <repo>/integrations/pi`. Restart pi (or a bb thread) to
load it. Without `--allow` (and no interactive input) no list is created and nothing may use the
MCP.

## Uninstall

```sh
integrations/pi/install.sh --uninstall
```

This removes only the symlink. Delete the allow-list, token and exports yourself if you want:
`rm -rf ~/.config/telegram-mcp ~/.local/share/telegram-mcp`.

## Tests

```sh
integrations/pi/tests/verify.sh
VERIFY_REAL_MODEL=<provider>/<model> integrations/pi/tests/verify.sh
```

Runs the MCP HTTP app against a fake Telegram account and a fake OpenAI-compatible server:
401 without the token, all eleven tools active for an allow-listed exact model, an allow-listed
model id containing `/` and an allow-listed `provider/*`, no `telegram_*` tools for a
non-matching model, an unlisted provider or a missing allow-list file, and execution-time
refusal for a non-allowed model. The final real-model call runs only when `VERIFY_REAL_MODEL` is
set; it uses that one local model and the fake MCP backend. No cloud model and no real Telegram
account are involved.
