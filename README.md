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
