from dotenv import load_dotenv
from typing import Callable, Awaitable
import os
import openai


load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")


def get_wav_file(file_ogg: str) -> str:
  from pydub import AudioSegment
  file_wav = file_ogg.replace('.ogg','.wav')
  x = AudioSegment.from_file(file_ogg)
  x.export(file_wav, format='wav')
  return file_wav


def recognize(file_wav: str) -> str:
  response = openai.Audio.transcribe(
    model="whisper-1",
    file=open(file_wav, 'rb'),
    response_format='text',
  )
  return response

async def handle(file_ogg: str, log: Callable[[str], Awaitable[None]]) -> str:
  file_wav = None

  try:
    await log("Downloading and converting...")
    file_wav = get_wav_file(file_ogg)

    await log("Text recognizing...")
    text = recognize(file_wav)

    return text
  finally:
    if file_wav and os.path.exists(file_wav):
      os.remove(file_wav)


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
