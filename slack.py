import os
import logging
from typing import final
import requests
import tempfile
import time

from dotenv import load_dotenv
import ngrok
import openai
from moviepy.editor import AudioFileClip
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

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
    slack_token = os.getenv("SLACK_TOKEN")
    slack_signing_secret = os.getenv("SLACK_SIGNING_SECRET")

    app = AsyncApp(token=slack_token, signing_secret=slack_signing_secret)

    @app.event("message")
    async def message_handler(event, client: AsyncWebClient):
        channel_id = event["channel"]
        files = event.get("files", [])
        ts = event["ts"]

        for file in files:
            if file["subtype"] == "slack_audio":
                response = requests.get(file["aac"], headers={"Authorization": f"Bearer {slack_token}", "Accept": "video/mp4"})
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

load_dotenv()

openai.api_key = os.getenv("OPENAI_API_KEY")
openai.organization = os.getenv("OPENAI_ORGANIZATION")

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.StreamHandler())
app = create_app()
tunnel = ngrok.connect("localhost:3002", domain="rare-bullfrog-pleasantly.ngrok-free.app", authtoken=os.getenv("NGROK_TOKEN"))
app.start(3002)