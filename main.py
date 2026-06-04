import os

import discord
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# Slová, na ktoré má Jonáš reagovať aj bez reálneho Discord označenia
ALIASES = ["jony", "jonas", "jonáš"]

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Jonáš je online ako {client.user}")


@client.event
async def on_message(message):
    # Bot neodpovedá sám sebe
    if message.author == client.user:
        return

    # Debug výpis: uvidíme v termináli, či bot vôbec číta správy
    print(f"Správa od {message.author}: {message.content!r}")

    text = message.content.lower().strip()

    # Reálne označenie bota cez Discord @mention
    mentioned_bot = client.user in message.mentions

    # Textové prezývky: jony, jonas, jonáš
    alias_used = any(alias in text for alias in ALIASES)

    if mentioned_bot or alias_used:
        await message.channel.send(
            "Som tu. Couple GlowUp začína. "
            "Dnes ešte netrénujeme, dnes nastavujeme systém. Žiadne výhovorky."
        )


if TOKEN is None:
    raise RuntimeError("Chýba DISCORD_TOKEN v súbore .env")

client.run(TOKEN)