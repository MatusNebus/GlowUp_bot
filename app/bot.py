import re

import discord

from app.config import DISCORD_TOKEN
from app.services.commitments_service import list_commitments, set_commitment
from app.services.joker_service import JOKER_FORMAT_MESSAGE, joker_status, use_joker
from app.services.planning_service import (
    PLAN_FORMAT_MESSAGE,
    add_plan,
    list_all_week,
    list_my_week,
    weekly_status,
)
from app.services.stats_service import (
    MONTH_FORMAT_MESSAGE,
    get_all_month_stats,
    get_user_month_stats,
)
from app.services.users_service import list_users, register_user
from app.services.workout_service import (
    complete_workout,
    get_workout_detail,
    miss_workout,
    shorten_workout,
)


# Textové aliasy, na ktoré má Jonáš reagovať aj bez reálneho Discord označenia.
ALIASES = ("jony", "jonas", "jonáš")

DONE_FORMAT_MESSAGE = (
    "Správny formát je: jonas done <plan_id> <výsledok>, "
    "napríklad: jonas done 3 5.2 32"
)
SHORT_FORMAT_MESSAGE = (
    "Správny formát je: jonas short <plan_id> <výsledok>, "
    "napríklad: jonas short 3 3.0 20"
)
MISSED_FORMAT_MESSAGE = (
    "Správny formát je: jonas missed <plan_id>, napríklad: jonas missed 3"
)
WORKOUT_FORMAT_MESSAGE = (
    "Správny formát je: jonas workout <plan_id>, napríklad: jonas workout 3"
)

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


def _format_commitments(discord_user_id: str | None = None) -> str:
    commitments = list_commitments(discord_user_id)
    if not commitments:
        return "Zatiaľ nie sú nastavené žiadne tréningové záväzky."

    lines = ["Tréningové záväzky:"]
    for commitment in commitments:
        lines.append(
            "- "
            f"{commitment['display_name']}: "
            f"{commitment['workout_type']} "
            f"{commitment['count_per_week']}x týždenne"
        )

    return "\n".join(lines)


def _parse_commitment_command(command_text: str) -> tuple[str, int] | None:
    parts = command_text.split()
    if len(parts) != 3:
        return None

    _, workout_type, count_text = parts
    try:
        count_per_week = int(count_text)
    except ValueError:
        return None

    return workout_type, count_per_week


def _parse_plan_command(command_text: str) -> tuple[str, str, str] | None:
    parts = command_text.split()
    if len(parts) != 4:
        return None

    _, workout_type, planned_day, planned_time = parts
    return workout_type, planned_day, planned_time


def _parse_result_command(command_text: str, command_name: str) -> tuple[int, str] | None:
    prefix = f"{command_name} "
    if not command_text.casefold().startswith(prefix):
        return None

    payload = command_text[len(prefix) :].strip()
    if not payload:
        return None

    plan_id_text, _, result_text = payload.partition(" ")
    if not plan_id_text.isdigit() or not result_text.strip():
        return None

    return int(plan_id_text), result_text.strip()


def _parse_plan_id_command(command_text: str, command_name: str) -> int | None:
    parts = command_text.split()
    if len(parts) != 2 or parts[0].casefold() != command_name:
        return None

    if not parts[1].isdigit():
        return None

    return int(parts[1])


def _parse_joker_command(command_text: str) -> tuple[int, str, str] | None:
    parts = command_text.split()
    if len(parts) != 4 or parts[0].casefold() != "joker":
        return None

    _, plan_id_text, new_day, new_time = parts
    if not plan_id_text.isdigit():
        return None

    return int(plan_id_text), new_day, new_time


def _parse_stats_command(command_text: str) -> tuple[bool, str | None] | None:
    parts = command_text.split()
    if not parts or parts[0].casefold() not in {"stats", "report"}:
        return None

    arguments = parts[1:]
    if not arguments:
        return False, None

    if arguments[0].casefold() == "all":
        if len(arguments) > 2:
            return None
        return True, arguments[1] if len(arguments) == 2 else None

    if len(arguments) == 1:
        return False, arguments[0]

    return None


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
            "jonas register Ema, jonas users, jonas commitment beh 2, "
            "jonas commitments, jonas commitments all, "
            "jonas plan beh piatok 18:00, jonas my week, jonas week, "
            "jonas planning status, jonas done 3 5.2 32, "
            "jonas done 4 drepy 3x12; kliky 3x8, jonas short 3 3.0 20, "
            "jonas missed 3, jonas workout 3, jonas joker 3 sobota 10:00, "
            "jonas joker status, jonas stats, jonas stats 2026-06, "
            "jonas stats all, jonas report all"
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

    if normalized_command.startswith("commitment "):
        parsed_commitment = _parse_commitment_command(command_text)
        if parsed_commitment is None:
            await message.channel.send(
                "Nerozumiem záväzku. Skús napríklad: jonas commitment beh 2"
            )
            return

        workout_type, count_per_week = parsed_commitment
        _, response = set_commitment(
            str(message.author.id), workout_type, count_per_week
        )
        await message.channel.send(response)
        return

    if normalized_command == "commitments":
        await message.channel.send(_format_commitments(str(message.author.id)))
        return

    if normalized_command == "commitments all":
        await message.channel.send(_format_commitments())
        return

    if normalized_command == "planning status":
        _, response = weekly_status(str(message.author.id))
        await message.channel.send(response)
        return

    if normalized_command == "plan" or normalized_command.startswith("plan "):
        parsed_plan = _parse_plan_command(command_text)
        if parsed_plan is None:
            await message.channel.send(PLAN_FORMAT_MESSAGE)
            return

        workout_type, planned_day, planned_time = parsed_plan
        _, response = add_plan(
            str(message.author.id), workout_type, planned_day, planned_time
        )
        await message.channel.send(response)
        return

    if normalized_command == "my week":
        _, response = list_my_week(str(message.author.id))
        await message.channel.send(response)
        return

    if normalized_command == "week":
        await message.channel.send(list_all_week())
        return

    if normalized_command == "joker status":
        _, response = joker_status(str(message.author.id))
        await message.channel.send(response)
        return

    if normalized_command == "joker" or normalized_command.startswith("joker "):
        parsed_joker = _parse_joker_command(command_text)
        if parsed_joker is None:
            await message.channel.send(JOKER_FORMAT_MESSAGE)
            return

        plan_id, new_day, new_time = parsed_joker
        _, response = use_joker(str(message.author.id), plan_id, new_day, new_time)
        await message.channel.send(response)
        return

    if normalized_command.startswith("stats") or normalized_command.startswith(
        "report"
    ):
        parsed_stats = _parse_stats_command(command_text)
        if parsed_stats is None:
            await message.channel.send(MONTH_FORMAT_MESSAGE)
            return

        show_all, month = parsed_stats
        if show_all:
            await message.channel.send(get_all_month_stats(month))
        else:
            _, response = get_user_month_stats(str(message.author.id), month)
            await message.channel.send(response)
        return

    if normalized_command == "done" or normalized_command.startswith("done "):
        parsed_result = _parse_result_command(command_text, "done")
        if parsed_result is None:
            await message.channel.send(DONE_FORMAT_MESSAGE)
            return

        plan_id, result_text = parsed_result
        _, response = complete_workout(str(message.author.id), plan_id, result_text)
        await message.channel.send(response)
        return

    if normalized_command == "short" or normalized_command.startswith("short "):
        parsed_result = _parse_result_command(command_text, "short")
        if parsed_result is None:
            await message.channel.send(SHORT_FORMAT_MESSAGE)
            return

        plan_id, result_text = parsed_result
        _, response = shorten_workout(str(message.author.id), plan_id, result_text)
        await message.channel.send(response)
        return

    if normalized_command == "missed" or normalized_command.startswith("missed "):
        plan_id = _parse_plan_id_command(command_text, "missed")
        if plan_id is None:
            await message.channel.send(MISSED_FORMAT_MESSAGE)
            return

        _, response = miss_workout(str(message.author.id), plan_id)
        await message.channel.send(response)
        return

    if normalized_command == "workout" or normalized_command.startswith("workout "):
        plan_id = _parse_plan_id_command(command_text, "workout")
        if plan_id is None:
            await message.channel.send(WORKOUT_FORMAT_MESSAGE)
            return

        _, response = get_workout_detail(str(message.author.id), plan_id)
        await message.channel.send(response)
        return

    await message.channel.send(
        "Som tu, ale tento príkaz ešte nepoznám. Skús: jonas help"
    )


def run_bot() -> None:
    client.run(DISCORD_TOKEN)
