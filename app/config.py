import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "Europe/Bratislava")

if not DISCORD_TOKEN:
    raise RuntimeError("Chýba DISCORD_TOKEN v súbore .env")
