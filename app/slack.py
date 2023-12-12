import os
import logging
import requests
import sqlite3
import tempfile

from dotenv import load_dotenv
from moviepy.editor import AudioFileClip
import ngrok
import openai
from slack_bolt.async_app import AsyncApp
from slack_bolt.oauth.async_oauth_settings import AsyncOAuthSettings
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.oauth.state_store.sqlite3 import SQLite3OAuthStateStore
from slack_sdk.oauth.installation_store.sqlite3 import SQLite3InstallationStore

TEN_MINUTES = 10 * 60 * 1000

def get_mp3_file(file_mp4: str) -> str:
    file_mp3 = file_mp4.replace(".mp4", ".mp3")
    clip = AudioFileClip(file_mp4)
    clip.write_audiofile(file_mp3)
    clip.close()
    return file_mp3



def recognize(file_mp3: str) -> str:
  response = openai.Audio.transcribe(
    model="whisper-1",
    file=open(file_mp3, "rb"),
    response_format="text",
    prompt="Вот мой ответ. Он может быть на русском, or maybe english or a combination of both. Иногда английские слова могут встречаться в русском тексте, такие как usecases, language models, etc. Some text may also be adjusted muuuuch better match the intonation. It's reeeeaally convenient."
  )
  return response   # type: ignore

def create_app() -> AsyncApp:
    slack_client_id = os.getenv("SLACK_CLIENT_ID")
    slack_client_secret = os.getenv("SLACK_CLIENT_SECRET")
    slack_token = os.getenv("SLACK_TOKEN")
    slack_signing_secret = os.getenv("SLACK_SIGNING_SECRET")

    assert slack_client_id is not None

    sqlite3.connect("./data/sqlite3/database.db").close()

    oauth_settings = AsyncOAuthSettings(
        client_id=slack_client_id,
        client_secret=slack_client_secret,
        scopes=["im:history", "channels:history", "groups:history"],
        redirect_uri=os.getenv("REDIRECT_URI"),
        installation_store = SQLite3InstallationStore(database="./data/sqlite3/database.db", client_id=slack_client_id),
        state_store = SQLite3OAuthStateStore(database="./data/sqlite3/database.db", expiration_seconds=120)
    )

    app = AsyncApp(signing_secret=slack_signing_secret, oauth_settings=oauth_settings)

    @app.event("message")
    async def message_handler(event, client: AsyncWebClient):
        channel_id = event["channel"]
        files = event.get("files", [])
        ts = event["ts"]

        for file in files:
            if file["subtype"] == "slack_audio":
                response = requests.get(file["aac"], headers={"Authorization": f"Bearer {client.token}", "Accept": "video/mp4"})
                mp3_file = None
                try:
                    with tempfile.NamedTemporaryFile("wb", suffix=".mp4") as f:
                        f.write(response.content)
                        mp3_file = get_mp3_file(f.name)
                        text = recognize(mp3_file)
                    await client.chat_postMessage(channel=channel_id, thread_ts=ts, text=f"_{text.strip()}_", mrkdwn=True)
                finally:
                    if mp3_file and os.path.exists(mp3_file):
                        os.remove(mp3_file)

    return app


if __name__ == "__main__":
    load_dotenv()

    openai.api_key = os.getenv("OPENAI_API_KEY")
    openai.organization = os.getenv("OPENAI_ORGANIZATION")

    ngrok_enabled = os.getenv("NGROK_ENABLED") == "true"

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    logger.addHandler(logging.StreamHandler())
    app = create_app()
    if ngrok_enabled:
        tunnel = ngrok.connect("localhost:3002", domain="rare-bullfrog-pleasantly.ngrok-free.app", authtoken=os.getenv("NGROK_TOKEN"))
    app.start(3002)