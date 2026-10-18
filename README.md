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

## Read-only Telegram MCP server

The compose service `mcp` gives an AI client (Claude Code or any other
[MCP](https://modelcontextprotocol.io) client) read access to your account. It can list chats,
folders and forum topics, read and search messages, and export chats to files. It cannot send,
edit, delete, join or leave anything, and it never marks messages as read.

It uses the streamable HTTP transport at `http://127.0.0.1:8790/mcp`, published on the loopback
interface only. Requests whose `Host` or `Origin` header is not a loopback address are rejected
(DNS-rebinding protection).

### Setup

1. Configure `.env` as for the bot (only `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are needed here)
   and create the directories, writable by uid 1000:

   ```sh
   mkdir -p data/mcp exports
   ```

2. Log in once. The server has its **own** Telegram authorization in
   `data/mcp/telegram_mcp.session`; it never uses or even sees the bot's session:

   ```sh
   docker compose run --rm mcp telegram-mcp login
   ```

   Telegram shows this login as a separate device in *Settings → Devices* and sends the usual
   "new login" notice. Keep the session file secret.
3. Start it (or `docker compose up -d` for both services):

   ```sh
   docker compose up -d mcp
   docker compose logs -f mcp
   ```

   Without a logged-in session the service exits at once with a log line naming the login
   command (and Docker keeps restarting it until you log in). Like the bot, it exits and is
   restarted when the Telegram connection is lost for good or the watchdog fails.
4. Connect Claude Code (add `--scope user` to make it available in every project):

   ```sh
   claude mcp add --transport http telegram http://127.0.0.1:8790/mcp
   ```

`MCP_PORT` in `.env` changes the host port. Exports go to `./exports` (inside the container
`/exports`); set `MCP_EXPORT_HOST_DIR` to an absolute path to put them elsewhere. Tools return
host paths, so run `docker compose` from this directory or set `MCP_EXPORT_HOST_DIR`. Other
settings (response size limit, export limits, media size limit) are in
[.env.example](.env.example).

### Tools

Chats are addressed by numeric id (as returned by `list_dialogs`), `@username`, a `t.me` link or
the exact title. Invite links are refused: the server never joins anything. Dates are ISO 8601,
UTC unless an offset is given; a bare date in `until` includes that whole day. Every list is
limited (messages: 50 by default, at most 100 per call; one response stays under about 60,000
characters) and returns `next_offset`, `next_offset_id` or `next_cursor` for the next page.

| Tool | Parameters | Returns |
| --- | --- | --- |
| `list_folders` | – | folders: id, title, kind, flags (`contacts`, `non_contacts`, `groups`, `broadcasts`, `bots`, `exclude_muted`, `exclude_read`, `exclude_archived`), pinned / included / excluded chats |
| `list_folder_chats` | `folder_id` (0 = All chats), `limit`, `offset` | chats of the folder, computed from its rules like the official apps |
| `list_dialogs` | `chat_type` (user/bot/group/channel), `archived`, `unread_only`, `name_contains`, `limit`, `offset` | chats with unread, mention and reaction counters, muted/archived/pinned flags |
| `get_chat_info` | `chat` | title, usernames, type, members count, description, linked chat, forum flag |
| `list_forum_topics` | `chat`, `query`, `limit`, `cursor` | topics: id, title, unread count, closed/pinned, last activity |
| `read_messages` | `chat`, `limit`, `offset_id`, `min_id`, `max_id`, `since`, `until`, `topic_id`, `reply_to`, `oldest_first`, `max_text` | messages with sender, date, text, media type, reply-to, forward info, reactions, views |
| `get_messages` | `chat`, `ids`, `max_text` | the listed messages and the ids not found |
| `search_messages` | `chat`, `query`, `sender`, `since`, `until`, `media`, `topic_id`, `limit`, `offset_id`, `max_text` | matching messages of one chat |
| `search_global` | `query`, `since`, `until`, `media`, `chat_type` (user/group/channel), `limit`, `cursor`, `max_text` | matching messages from all chats |
| `export_chat` | `chat`, `since`, `until`, `topic_id`, `format` (json/markdown/both), `include_media`, `max_messages` | host paths of `messages.json` / `messages.md`, message count; media go to `media/` in the same folder |

`media` filters: `photo`, `video`, `photo_video`, `document`, `audio`, `voice`, `video_note`,
`voice_or_video_note`, `gif`, `url`, `poll`, `location`, `contact`, `pinned`, `mentions`.

### How read-only is enforced

The server's Telethon client (`transcription_bot/mcp_server/guard.py`) checks every outgoing
request against an explicit allow-list of read-only API functions (`messages.getHistory`,
`messages.search`, `messages.getDialogs`, `upload.getFile` and similar) before anything is
queued for sending. The check runs both in the client's request entry point and in the
connection objects themselves (the main one and the extra ones used to download media from other
data centers). Wrapper requests (`invokeWithLayer`, `initConnection`, `invokeWithoutUpdates`)
are unwrapped and their inner request is checked. Batches are rejected as a whole if one item is
not allowed, and unknown wrappers are rejected. Anything else raises an error instead of being
sent, including:

- `messages.readHistory`, `channels.readHistory`, `messages.readMentions`, `messages.readReactions`,
  `messages.readMessageContents` and `messages.readDiscussion`;
- `messages.getMessagesViews`, which increments view counters;
- `account.updateStatus`;
- any send, edit, delete, forward, react, join, leave, upload or folder change.

`send_read_acknowledge` is disabled too. The client runs with `receive_updates=False`, so
Telegram does not push updates to this session. The tests in `tests/test_mcp_guard.py` scan the
whole TL schema to check that no request that looks like a write is accepted. They also run
every tool through the real Telethon client and record what would have been sent.

The `login` command uses the same guard with five sign-in requests added (`auth.sendCode`,
`auth.resendCode`, `auth.signIn`, `account.getPassword`, `auth.checkPassword`).

### Telegram caveats when reading without marking as read

- Read state changes only through the explicit `read*` requests above. Reading history,
  searching, fetching messages by id or downloading media leaves the unread counters, mention and
  reaction badges, and the "listened" state of voice and video notes as they were.
- Channel views are counted only through `messages.getMessagesViews` with `increment`. Since that
  is blocked, nothing you read is counted as a view. The `views` field shows the value at fetch
  time.
- Online status is set only by `account.updateStatus`, which is blocked, so reading does not make
  you appear online. The session still appears under *Devices*, and its last-activity time and IP
  are updated by any request; this cannot be avoided.
- Stories and sponsored messages are never requested, so no story views or ad impressions are
  reported.
- When the session has no update state yet, Telethon fetches it on connect
  (`updates.getState` / `getDifference`). These only advance this session's own update position.
- Secret chats exist only on the devices that took part in them and are not visible through the
  API.
- Folder rules treat a chat as muted only if it is muted itself. A chat that is silent only
  because of the global notification default for its chat type counts as not muted.
- `contacts.resolveUsername` is heavily rate-limited by Telegram. Prefer numeric ids from
  `list_dialogs`. Telegram flood waits longer than `MCP_FLOOD_SLEEP_THRESHOLD` (30 s) are returned
  as a tool error instead of blocking the call.

## Local development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
uv run transcription-bot login   # session goes to ./data unless DATA_DIR is set
uv run transcription-bot         # reads .env from the current directory
uv run telegram-mcp login        # separate session for the MCP server (data/telegram_mcp.session)
uv run telegram-mcp              # MCP server on 127.0.0.1:8790, exports to ./exports
```
