# Voice message transcription for Telegram

A Telegram userbot (it logs in as your own account). When you send a voice message, it
transcribes it with the OpenAI transcription API (`whisper-1` by default) and puts the text into
the message caption. If the text does not fit into a caption, the caption becomes
"See transcription below" and the text is sent as a reply (split into several messages if needed).

Only voice messages you record yourself are touched (outgoing, not forwarded). Messages that
arrive while the bot is down are picked up on the next start, and interrupted transcriptions are
retried.

## Run with Docker

1. Get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` at https://my.telegram.org
   ([instructions](https://core.telegram.org/api/obtaining_api_id)) and an `OPENAI_API_KEY` at
   https://platform.openai.com/api-keys.
2. Create the config:

   ```sh
   cp .env.example .env    # fill in the values
   ```

   The session and the queue live in the Docker volume `bot-data` (see below), so no host
   directory and no file permissions are involved.
3. Log in to Telegram once (asks for the phone number, the login code and the 2FA password):

   ```sh
   docker compose run --rm bot transcription-bot login
   ```

   This creates `transcription_bot.session` in the volume. Keep it secret: it gives full access to
   the account. If the bot is already running, stop it first (`docker compose stop bot`): two
   processes must never use one session at the same time, because Telegram may then invalidate it.
   The bot takes a lock next to the session file and refuses to start ("another instance is
   running") while another process holds it.
4. Start the bot and watch the logs:

   ```sh
   docker compose up -d
   docker compose logs -f bot
   ```

   `docker compose up -d` starts only the bot; the MCP server below is a separate, optional
   profile.

The container restarts automatically (`restart: unless-stopped`). The bot exits with a non-zero
code, and Docker restarts it, when the Telegram connection is lost for good, the watchdog
(an `updates.GetState` request every 60 s) fails 3 times in a row, or the queue file cannot be
written. Without a logged-in session, while another instance holds the lock, or with missing
variables it exits at once (code 2) with a log line saying what is wrong.

Optional settings (model, session name, `OPENAI_BASE_URL` for an OpenAI-compatible server, log
level) are listed in [.env.example](.env.example). Timeouts and retry limits are constants in the
code.

### Retries and failures

- A temporary problem (network, Telegram or OpenAI outage, OpenAI rate limit, and also OpenAI
  account problems: invalid key, billing, `insufficient_quota`) is retried with a growing pause of
  up to 5 minutes, for 24 hours after the message was sent. Only then the caption becomes
  "Transcription failed: …". Telegram allows editing a message for 48 hours, so you can still fix
  the account and record again.
- A message that crashes or hangs the bot (or an unexpected error) is given up after 5 attempts,
  so one bad message cannot keep the bot in a restart loop. Stopping or restarting the bot does not
  count as a failed attempt.
- The transcript is stored in the queue as soon as OpenAI returns it, so a retry never asks
  OpenAI again, and a long transcript that is sent as several replies continues with the next
  reply after a restart instead of sending the earlier ones again.
- A message stays in the queue until its caption is set: if writing the failure note fails too,
  it is tried again later, including after a restart.

### Messages sent while the bot is down

On start, Telethon fetches the updates the session missed, and the bot then searches your recent
voice messages (`messages.searchGlobal` with the voice filter) for ones without a transcription,
from the last time the bot was running (a heartbeat in the queue file, at most 48 hours back). This
covers supergroups and channels, where Telethon's own catch-up can miss messages. The search is
best effort: if it fails, the bot logs a warning and works as usual, and the next start searches
again. On the very first start there is no heartbeat yet, so older voice messages are left alone.
Secret chats are not visible to the API at all.

### Data volume: inspect, back up, restore

The bot's files are in the named volume `bot-data` (Docker prefixes it with the project name,
see `docker volume ls`). Stop the bot before copying them:

```sh
docker compose stop bot
docker compose run --rm --no-deps bot ls -la /data                               # inspect
docker compose run --rm --no-deps -T bot tar cz -C /data . > bot-data.tgz        # back up
docker compose run --rm --no-deps -T bot tar xz -C /data < bot-data.tgz          # restore
docker compose start bot
```

`docker compose down` keeps the volumes; `docker compose down -v` deletes them (and with them the
session and the queue).

### Moving from the earlier `./data` layout

Earlier versions of this branch kept the files in `./data` (and `./data/mcp`) on the host. To move
them into the volumes once:

```sh
docker compose --profile mcp down
mkdir -p exports
docker compose run --rm --no-deps --user root -v "$PWD/data:/old:ro" bot \
    sh -c 'cp /old/*.session /old/queue.sqlite3 /data/; chown -R bot:bot /data'
docker compose run --rm --no-deps --user root -v "$PWD/data/mcp:/old:ro" mcp \
    sh -c 'cp /old/*.session /data/; chown -R bot:bot /data'   # only if you used the MCP server
sudo rm -rf data   # after checking the bot works; plain rm if the files are yours
```

Docker may have created `./data` or `./exports` as root on an earlier run. `./data` is no longer
used; if `./exports` is owned by root, run `sudo chown "$(id -u):$(id -g)" exports`. The MCP
server writes there as uid 1000; if your uid differs, make the directory writable for uid 1000.

## Read-only Telegram MCP server

The compose service `mcp` gives an AI client (Claude Code or any other
[MCP](https://modelcontextprotocol.io) client) read access to your account. It can list chats,
folders and forum topics, read and search messages, and export chats to files. It cannot send,
edit, delete, join or leave anything, and it never marks messages as read.

It uses the streamable HTTP transport at `http://127.0.0.1:8790/mcp`, published on the loopback
interface only. Requests whose `Host` or `Origin` header is not a loopback address are rejected
(DNS-rebinding protection). That is not access control: **without `MCP_AUTH_TOKEN` any process
on this machine (any user, any container) can read your whole account through it.** Set the token.

Message texts are written by other people. An AI client reads them as input, so a message can
try to instruct the model (prompt injection). The server itself cannot write anything, but the
client may have other tools; do not let it act on what it reads without your confirmation.

### Setup

1. Configure `.env` as for the bot (only `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are needed
   here) and add a token:

   ```sh
   echo "MCP_AUTH_TOKEN=$(openssl rand -hex 32)" >> .env
   mkdir -p exports        # export directory; must exist before the service starts
   ```

   The service refuses to start if `./exports` does not exist (so Docker never creates it as
   root). It must be writable by uid 1000, the container user.
2. Log in once. The server has its **own** Telegram authorization in its own volume `mcp-data`;
   it never uses or even sees the bot's session:

   ```sh
   docker compose run --rm mcp telegram-mcp login
   ```

   Telegram shows this login as a separate device in *Settings → Devices* and sends the usual
   "new login" notice. Stop the service before logging in again (`docker compose stop mcp`).
3. Start it:

   ```sh
   docker compose up -d mcp            # or: docker compose --profile mcp up -d (bot and mcp)
   docker compose logs -f mcp
   ```

   Without a logged-in session the service exits at once with a log line naming the login
   command (and Docker keeps restarting it until you log in). Like the bot, it exits and is
   restarted when the Telegram connection is lost for good or the watchdog fails. Use
   `docker compose --profile mcp down` to stop both services.
4. Connect Claude Code (add `--scope user` to make it available in every project):

   ```sh
   claude mcp add --transport http telegram http://127.0.0.1:8790/mcp \
       --header "Authorization: Bearer $(grep ^MCP_AUTH_TOKEN= .env | cut -d= -f2)"
   ```

`MCP_PORT` in `.env` changes the host port. Exports go to `./exports` (inside the container
`/exports`), relative to the directory you run `docker compose` in; set `MCP_EXPORT_HOST_DIR` to an
absolute path to put them elsewhere. The same value is mounted and reported by the tools, so the
returned paths are always right. Other settings (response size limit, export limits, media size
limits) are in [.env.example](.env.example).

### Tools

Chats are addressed by numeric id (as returned by `list_dialogs`), `@username`, a `t.me` link or
the exact title. Invite links are refused: the server never joins anything. Dates are ISO 8601,
UTC unless an offset is given; a bare date in `until` includes that whole day. Every list is
limited (messages: 50 by default, at most 100 per call; one response stays under about 60,000
characters) and returns `next_offset`, `next_offset_id` or `next_cursor` for the next page.
Chat lists and their counters are cached for up to 60 s.

One `export_chat` call stops after about 50 seconds (MCP clients time out long tool calls) and
after `MCP_EXPORT_MAX_TOTAL_MB` (1000 MB) of media; later media are listed as skipped. A partial
export says `truncated: true` with the reason and `resume_after_id`; calling `export_chat` again
with the same arguments and `after_id` set to it writes the next part into a new folder.

| Tool | Parameters | Returns |
| --- | --- | --- |
| `list_folders` | `offset` | folders: id, title, kind, flags (`contacts`, `non_contacts`, `groups`, `broadcasts`, `bots`, `exclude_muted`, `exclude_read`, `exclude_archived`), pinned / included / excluded chats |
| `list_folder_chats` | `folder_id` (0 = All chats), `limit`, `offset` | chats of the folder, computed from its rules like the official apps |
| `list_dialogs` | `chat_type` (user/bot/group/channel), `archived`, `unread_only`, `name_contains`, `limit`, `offset` | chats with unread, mention and reaction counters, muted/archived/pinned flags |
| `get_chat_info` | `chat` | title, usernames, type, members count, description, linked chat, forum flag |
| `list_forum_topics` | `chat`, `query`, `limit`, `cursor` | topics: id, title, unread count, closed/pinned, last activity |
| `read_messages` | `chat`, `limit`, `offset_id`, `min_id`, `max_id`, `since`, `until`, `topic_id`, `reply_to`, `oldest_first`, `max_text` | messages with sender, date, text, media type, reply-to, forward info, reactions, views |
| `get_messages` | `chat`, `ids`, `max_text` | the listed messages and the ids not found |
| `search_messages` | `chat`, `query`, `sender`, `since`, `until`, `media`, `topic_id`, `limit`, `offset_id`, `max_text` | matching messages of one chat |
| `search_global` | `query`, `since`, `until`, `media`, `chat_type` (user/group/channel), `limit`, `cursor`, `max_text` | matching messages from all chats |
| `export_chat` | `chat`, `since`, `until`, `topic_id`, `format` (json/markdown/both), `include_media`, `max_messages`, `after_id` | host paths of `messages.json` / `messages.md`, message count; media go to `media/` in the same folder |

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
every tool through the real Telethon client and record what would have been sent. Telethon is
pinned to 1.45.x, the version the guard was reviewed against, and a test compares every place in
Telethon's sources that creates a connection or sends a request with the reviewed list, so an
upgrade fails loudly until the guard is reviewed again.

The `login` command uses the same guard with five sign-in requests added (`auth.sendCode`,
`auth.resendCode`, `auth.signIn`, `account.getPassword`, `auth.checkPassword`).

### Telegram caveats when reading without marking as read

- Read state changes only through the explicit `read*` requests above. Reading history,
  searching, fetching messages by id or downloading media leaves the unread counters, mention and
  reaction badges, and the "listened" state of voice and video notes as they were.
- Channel views are counted only through `messages.getMessagesViews` with `increment`. Since that
  is blocked, nothing you read is counted as a view. The `views` field shows the value at fetch
  time.
- This server never sends `account.updateStatus`, the request that sets the online status. Whether
  Telegram also counts other API activity as being online has not been verified, so contacts may
  still see recent activity. The session appears under *Devices*, and its last-activity time and
  IP are updated by any request; this cannot be avoided.
- Stories and sponsored messages are never requested, so no story views or ad impressions are
  reported.
- When the session has no update state yet, Telethon fetches it on connect
  (`updates.getState` / `getDifference`). These only advance this session's own update position.
- Secret chats exist only on the devices that took part in them and are not visible through the
  API.
- A chat counts as muted if it is muted itself or, when it has no setting of its own, if the
  default for its chat type (private chats, groups, channels) is muted. The defaults are read with
  `account.getNotifySettings`, a pure read.
- `contacts.resolveUsername` is heavily rate-limited by Telegram. Prefer numeric ids from
  `list_dialogs`. Telegram flood waits longer than `MCP_FLOOD_SLEEP_THRESHOLD` (30 s) are returned
  as a tool error instead of blocking the call.

## Local development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
export DATA_DIR=data-dev         # local session and queue, separate from the Docker volumes
uv run transcription-bot login
uv run transcription-bot         # reads .env from the current directory
uv run telegram-mcp login        # separate session for the MCP server (data-dev/telegram_mcp.session)
uv run telegram-mcp              # MCP server on 127.0.0.1:8790, exports to ./exports
```

Do not run a local bot while the Docker bot is running on the same account: both would try to
transcribe the same messages.
