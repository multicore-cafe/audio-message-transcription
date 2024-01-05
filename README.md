1. install [`poetry`](https://python-poetry.org/docs/#installation)
2. install [`ffmpeg`](https://ffmpeg.org/download.html)
3. `cp .env.example .env`
4. Fill `.env` file with values:
    - get `OPENAI_API_KEY` from https://platform.openai.com/account/api-keys
    - get `OPENAI_ORGANIZATION`from https://platform.openai.com/account/organization
    - get `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` using https://core.telegram.org/api/obtaining_api_id
    - get `SLACK_SIGNING_SECRET`, `SLACK_CLIENT_SECRET`and `SLACK_CLIENT_ID` from Basic Information tab of https://api.slack.com/apps
5. `poetry install`
6. Telegram bot: `poetry run python app/tg.py` and auth; Slack bot: `poetry run python app/slack.py`

# Notes on distribution.

For OAuth to work properly, the app should be distributed via this link: https://sharp-foxhound-probably.ngrok-free.app/slack/install

# Setting up the APP in the slack API GUI

In this section I describe what should be done in the slack API GUI to make the app work.

TODO


### OAuth & Permissions

Set Redirect URL to your app destination + `/slack/oauth_redirect`, for example:

`https://sharp-foxhound-probably.ngrok-free.app/slack/oauth_redirect`

Add all the scopes mentioned in the `slack.py`

### Event Subscriptions

Set the Request URL to your app events location, for example:

`https://sharp-foxhound-probably.ngrok-free.app/slack/events`

Subscribe to the following bot events:
`message.channels, message.groups, message.in, message.mpim`
