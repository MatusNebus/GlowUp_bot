import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Bratislava")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
ADMIN_DISCORD_USER_ID = os.getenv("ADMIN_DISCORD_USER_ID")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

if not DISCORD_TOKEN:
    raise RuntimeError("Chýba DISCORD_TOKEN v súbore .env")
