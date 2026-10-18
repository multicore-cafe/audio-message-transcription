# Voice message transcription for Telegram

A Telegram userbot (it logs in as your own account). When you send a voice message, it
transcribes it with the OpenAI transcription API (`whisper-1` by default) and puts the text into
the message caption. If the text does not fit into a caption, the caption becomes
"See transcription below" and the text is sent as a reply (split into several messages if needed).

Only your own outgoing voice messages are touched. Messages that arrive while the bot is down
are picked up on the next start, and interrupted transcriptions are retried.

## Run with Docker

1. Get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` at https://my.telegram.org
   ([instructions](https://core.telegram.org/api/obtaining_api_id)) and an `OPENAI_API_KEY` at
   https://platform.openai.com/api-keys.
2. Create the config and the data directory:

   ```sh
   cp .env.example .env    # fill in the values
   mkdir -p data           # session and queue files live here
   ```

   `data/` must be writable by uid 1000 (the container user). If your user has another uid,
   set `BOT_UID` and `BOT_GID` in the shell or in a compose `.env` override.
3. Log in to Telegram once (asks for the phone number, the login code and the 2FA password):

   ```sh
   docker compose run --rm bot transcription-bot login
   ```

   This creates `data/transcription_bot.session`. Keep it secret: it gives full access to the account.
4. Start the bot and watch the logs:

   ```sh
   docker compose up -d
   docker compose logs -f bot
   ```

The container restarts automatically (`restart: unless-stopped`). The bot exits with a non-zero
code, and Docker restarts it, when the Telegram connection is lost for good or the watchdog
(an `updates.GetState` request every 60 s) fails 3 times in a row. Without a logged-in session or
with missing variables it exits at once with a log line saying what is wrong.

Optional settings (model, session name, retry limits, `OPENAI_BASE_URL` for an
OpenAI-compatible server) are listed in [.env.example](.env.example). `TELEGRAM_SESSION` sets the
session file name, so another service can use its own session in the same `data/` directory.

## Local development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
uv run transcription-bot login   # session goes to ./data unless DATA_DIR is set
uv run transcription-bot         # reads .env from the current directory
```
