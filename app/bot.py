import re

import discord

from app.config import DISCORD_TOKEN
from app.services.users_service import list_users, register_user


# Textové aliasy, na ktoré má Jonáš reagovať aj bez reálneho Discord označenia.
ALIASES = ("jony", "jonas", "jonáš")

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


def _find_text_alias(text: str) -> tuple[int, str] | None:
    normalized_text = text.casefold()

    matches = []
    for alias in ALIASES:
        index = normalized_text.find(alias.casefold())
        if index != -1:
            matches.append((index, alias))

    if not matches:
        return None

    return min(matches, key=lambda match: match[0])


def _find_mention(text: str) -> tuple[int, str] | None:
    if client.user is None:
        return None

    mention_pattern = re.compile(rf"<@!?{client.user.id}>")
    match = mention_pattern.search(text)
    if match is None:
        return None

    return match.start(), match.group(0)


def _extract_command_text(message: discord.Message) -> str | None:
    text = message.content.strip()
    triggers = []

    alias_match = _find_text_alias(text)
    if alias_match is not None:
        triggers.append(alias_match)

    mention_match = _find_mention(text)
    if mention_match is not None:
        triggers.append(mention_match)

    if not triggers:
        return None

    trigger_index, trigger_value = min(triggers, key=lambda trigger: trigger[0])
    command_start = trigger_index + len(trigger_value)
    return text[command_start:].strip()


def _format_users() -> str:
    users = list_users()
    if not users:
        return "Zatiaľ nie je nikto registrovaný."

    names = [user["display_name"] for user in users]
    return "Registrovaní používatelia: " + ", ".join(names)


@client.event
async def on_ready() -> None:
    print(f"Jonáš je online ako {client.user}")


@client.event
async def on_message(message: discord.Message) -> None:
    # Bot ignoruje vlastné správy.
    if message.author == client.user:
        return

    command_text = _extract_command_text(message)
    if command_text is None:
        return

    normalized_command = command_text.casefold()

    if normalized_command == "help":
        await message.channel.send(
            "Príkazy: jonas help, jonas ping, jonas register Matúš, "
            "jonas register Ema, jonas users"
        )
        return

    if normalized_command == "ping":
        await message.channel.send("Som online. Žiadne výhovorky.")
        return

    if normalized_command.startswith("register "):
        display_name = command_text[len("register ") :].strip()
        _, response = register_user(str(message.author.id), display_name)
        await message.channel.send(response)
        return

    if normalized_command == "users":
        await message.channel.send(_format_users())
        return

    await message.channel.send(
        "Som tu, ale tento príkaz ešte nepoznám. Skús: jonas help"
    )


def run_bot() -> None:
    client.run(DISCORD_TOKEN)
