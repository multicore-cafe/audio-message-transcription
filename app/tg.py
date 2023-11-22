from dotenv import load_dotenv
from typing import Callable, Awaitable
import os
import openai


load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")


TEN_MINUTES = 10 * 60 * 1000


def get_mp3_file(file_ogg: str) -> str:
  from pydub import AudioSegment
  file_mp3 = file_ogg.replace(".ogg", ".mp3")
  segment = AudioSegment.from_file(file_ogg)[:TEN_MINUTES]
  segment.export(file_mp3, format="mp3")
  return file_mp3


def recognize(file_mp3: str) -> str:
  response = openai.Audio.transcribe(
    model="whisper-1",
    file=open(file_mp3, 'rb'),
    response_format='text',
    prompt="Вот мой ответ. Он может быть на русском, or maybe english or a combination of both. Иногда английские слова могут встречаться в русском тексте, такие как usecases, language models, etc. Some text may also be adjusted muuuuch better match the intonation. It's reeeeaally convenient."
  )
  return response

async def handle(file_ogg: str, log: Callable[[str], Awaitable[None]]) -> str:
  file_mp3 = None

  try:
    await log("Downloading and converting...")
    file_mp3 = get_mp3_file(file_ogg)

    await log("Text recognizing...")
    text = recognize(file_mp3)

    return text
  finally:
    if file_mp3 and os.path.exists(file_mp3):
      os.remove(file_mp3)


def start_bot():
  from pyrogram import Client, filters
  from pyrogram.types import Message
  from pyrogram.handlers import MessageHandler

  async def handle_voice(client: Client, message: Message):
    file_ogg = None

    async def log(msg: str):
      await message.edit_text("__" + msg + "__")

    try:
      file_ogg = await client.download_media(message.voice)
      result = await handle(file_ogg, log)
      await message.edit_text(result)
    except Exception as e:
      message.edit_text("")
      raise e
    finally:
      if file_ogg and os.path.exists(file_ogg):
        os.remove(file_ogg)

  app = Client("text", os.getenv("TELEGRAM_API_ID"), os.getenv("TELEGRAM_API_HASH"))
  app.add_handler(MessageHandler(handle_voice, filters.voice & filters.me))
  app.run()


start_bot()
