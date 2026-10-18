# Voice message transcription for Telegram

A Telegram userbot that logs in as your own account: when you send a voice message it transcribes
it with an OpenAI-compatible Whisper API and puts the text into the message caption (long texts
become replies). It also ships an optional read-only Telegram MCP server for AI clients.
Bot and MCP server are separate Compose services with separate Telegram sessions.

## Setup

1. Get `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` at https://my.telegram.org and an `OPENAI_API_KEY`
   (or point `OPENAI_BASE_URL` at a compatible endpoint), then:

   ```sh
   cp .env.example .env    # fill in the values
   docker compose up -d bot
   docker compose logs -f bot
   ```

   `docker-compose.override.yml` (host-specific, git-ignored) joins the bot to the external
   `whisper_default` network so the local Speaches server is reachable as
   `OPENAI_BASE_URL=http://whisper-speaches:8000/v1`; see `.env.example` for the commented example.

2. Log in once. The session and queue live in the `bot-data` volume:

   ```sh
   docker compose run --rm bot transcription-bot login
   ```

   Keep the session secret: it gives full access to the account. Stop the bot before logging in
   again (`docker compose stop bot`); two processes must never use one session, and the bot takes
   a lock next to the session file. `docker compose down` keeps the volumes; `down -v` deletes
   them. Back up the data volume with the bot stopped:

   ```sh
   docker compose stop bot
   docker compose run --rm --no-deps -T bot tar cz -C /data . > bot-data.tgz
   docker compose start bot
   ```

   The bot restarts automatically (`restart: unless-stopped`) and exits with code 2 (plus a log
   line) on bad configuration or a missing session. Retry and watchdog behavior is documented in
   the code (`transcription_bot/processor.py`, `watchdog.py`).

## Read-only Telegram MCP server

The optional `mcp` service has 10 read tools: list folders, folder chats, dialogs and forum
topics; chat info; read/get/search messages per chat and globally; export a chat. It can never
send, edit, delete, join or leave, and reading never marks anything as read. Every outgoing API
request is checked against a read-only allow-list (`transcription_bot/mcp_server/guard.py`).

It is **off by default**. Enable it with `.env` and the install script (token file, export
directory and the pi extension):

```sh
./integrations/pi/install.sh         # creates ~/.config/telegram-mcp/token (0600) and the exports dir
echo 'TELEGRAM_MCP_ENABLED=1' >> .env
docker compose run --rm mcp telegram-mcp login   # the server's own session, separate from the bot
docker compose up -d                 # now also starts the mcp container
```

Only the value `1` enables it; absent/`0` keeps the service scaled to 0 and makes
`telegram-mcp serve|login` exit 2 with "Telegram MCP is disabled" (any other value fails
`docker compose config`). Disable it again by setting `TELEGRAM_MCP_ENABLED=0` and running
`docker compose up -d`. The server listens on `127.0.0.1:8790/mcp` only.

### Access policy

- Only the local model **MiniCPM5-2B** (pi provider/model `minicpm/minicpm5-2b`) may use the MCP
  server. The pi extension in [`integrations/pi/`](integrations/pi/) activates its `telegram_*`
  tools only for that model and re-checks the model before every call; every other model must
  answer **"I can't do this: access to Telegram data is restricted to the local model."** The
  rules agents must follow are in [AGENTS.md](AGENTS.md).
- Use it from a bb thread with provider `pi` and model `minicpm/minicpm5-2b`; the Telegram tools
  appear automatically. Other models never see them.
- The bearer token is a file outside the repository, `~/.config/telegram-mcp/token` (mode 0600),
  mounted into the container as a Docker secret; it is never stored in `.env`. Rotate it with:
  `openssl rand -hex 32 > ~/.config/telegram-mcp/token && docker compose restart mcp`.
- Exports go to `~/.local/share/telegram-mcp/exports` (mode 0700), outside the repository.
- Uninstall the pi extension with `./integrations/pi/install.sh --uninstall`.
- Threat model: this enforces policy for models, not for processes. Every agent runs as your OS
  user, so a cloud model with shell access could still read the token file, the session volumes
  or the exports; only a separate OS user or container would prevent that.

## Local development

```sh
uv sync
uv run pytest
DATA_DIR=data-dev uv run transcription-bot login && DATA_DIR=data-dev uv run transcription-bot
TELEGRAM_MCP_ENABLED=1 DATA_DIR=data-dev uv run telegram-mcp   # local MCP server (exports to ./exports)
```

Do not run a local bot while the Docker bot runs on the same account. Tests use fake Telegram
clients and never touch the live account.
