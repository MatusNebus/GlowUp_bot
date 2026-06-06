import asyncio
import json
import re

import discord

from app.ai_parser import (
    AI_NOT_CONFIGURED_MESSAGE,
    OpenAIKeyMissingError,
    parse_natural_message,
)
from app.config import DISCORD_TOKEN
from app.services.commitments_service import list_commitments, set_commitment
from app.services.context_service import build_ai_context, save_user_message
from app.services.joker_service import JOKER_FORMAT_MESSAGE, joker_status, use_joker
from app.services.pending_actions_service import (
    build_pending_context,
    create_pending_action,
    format_pending_action,
    get_latest_pending_action,
    resolve_pending_action,
)
from app.services.planning_service import (
    PLAN_FORMAT_MESSAGE,
    add_plan,
    list_all_week,
    list_my_week,
    weekly_status,
)
from app.services.scheduler_service import send_scheduler_test_messages, start_scheduler
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


async def _parse_with_ai(
    message_text: str,
    author_display_name: str,
    context_text: str,
    pending_action_text: str,
) -> dict | None:
    try:
        return await asyncio.to_thread(
            parse_natural_message,
            message_text,
            author_display_name,
            context_text,
            pending_action_text,
        )
    except OpenAIKeyMissingError:
        return None
    except Exception as error:
        print(f"OpenAI parser chyba: {error}")
        raise


async def _execute_ai_intent(message: discord.Message, parsed: dict) -> bool:
    discord_user_id = str(message.author.id)
    intent = parsed["intent"]

    if intent == "unknown" and _has_multiple_plan_options(parsed):
        await message.channel.send(
            "Našiel som viac možností. Pozri si ID cez: jonas my week"
        )
        return False

    if intent == "plan_workout":
        if not parsed["day"] or not parsed["time"]:
            await message.channel.send(
                "Potrebujem deň aj čas. Napíš napríklad: jonas v piatok o 18:00 beh"
            )
            return False
        if not parsed["workout_type"]:
            await message.channel.send(
                "Rozumiem, že niečo chceš, ale nemám dosť údajov. "
                "Skús to napísať konkrétnejšie: typ tréningu, deň, čas alebo ID tréningu."
            )
            return False
        success, response = add_plan(
            discord_user_id,
            parsed["workout_type"],
            parsed["day"],
            parsed["time"],
        )
        await message.channel.send(response)
        return success

    if intent in {"log_done", "log_short", "log_missed", "use_joker"}:
        if parsed["plan_id"] is None:
            if _has_workout_description(message.content, parsed):
                response = (
                    "Skúsim to nájsť podľa plánu, ale neviem to určiť jednoznačne. "
                    "Pozri si ID cez: jonas my week"
                )
            else:
                response = "Potrebujem ID tréningu. Pozri si ho cez: jonas my week"
            await message.channel.send(response)
            return False

    if intent == "log_done":
        if not parsed["result_text"]:
            if parsed["workout_type"] == "beh":
                response = (
                    "Pri behu potrebujem kilometre a čas. Napríklad: "
                    "jonas tréning 3 hotový, 5.2 km za 32 min"
                )
            else:
                response = "Potrebujem aj výsledok tréningu."
            await message.channel.send(response)
            return False
        success, response = complete_workout(
            discord_user_id, parsed["plan_id"], parsed["result_text"]
        )
        await message.channel.send(response)
        return success

    if intent == "log_short":
        if not parsed["result_text"]:
            await message.channel.send("Potrebujem aj výsledok skráteného tréningu.")
            return False
        success, response = shorten_workout(
            discord_user_id, parsed["plan_id"], parsed["result_text"]
        )
        await message.channel.send(response)
        return success

    if intent == "log_missed":
        success, response = miss_workout(discord_user_id, parsed["plan_id"])
        await message.channel.send(response)
        return success

    if intent == "use_joker":
        if not parsed["day"] or not parsed["time"]:
            await message.channel.send(
                "Potrebujem nový deň aj čas. Napíš napríklad: "
                "jonas posuň tréning 4 na sobotu 10:00"
            )
            return False
        success, response = use_joker(
            discord_user_id, parsed["plan_id"], parsed["day"], parsed["time"]
        )
        await message.channel.send(response)
        return success

    if intent == "show_my_week":
        _, response = list_my_week(discord_user_id)
        await message.channel.send(response)
        return True

    if intent == "show_week":
        await message.channel.send(list_all_week())
        return True

    if intent == "show_planning_status":
        _, response = weekly_status(discord_user_id)
        await message.channel.send(response)
        return True

    if intent == "show_stats":
        _, response = get_user_month_stats(discord_user_id, parsed["month"])
        await message.channel.send(response)
        return True

    if intent == "show_stats_all":
        await message.channel.send(get_all_month_stats(parsed["month"]))
        return True

    if intent == "set_commitment":
        if not parsed["workout_type"] or parsed["count_per_week"] is None:
            await message.channel.send(
                "Potrebujem typ tréningu aj počet za týždeň. "
                "Napíš napríklad: jonas chcem behať 2x týždenne"
            )
            return False
        success, response = set_commitment(
            discord_user_id, parsed["workout_type"], parsed["count_per_week"]
        )
        await message.channel.send(response)
        return success

    if intent == "forbidden_walk_replacement":
        await message.channel.send(
            "Nie. Prechádzka sa podľa pravidiel Couple GlowUp neráta ako tréning "
            "a nemôže nahradiť plánovaný beh ani posilku. Môže byť bonus alebo "
            "regenerácia, ale povinný tréning ostáva."
        )
        return True

    if intent == "ask_matus_decision":
        question = parsed["decision_question"] or parsed["raw_summary"]
        await message.channel.send(f"Matúš, potrebujem rozhodnutie: {question}")
        return True

    await message.channel.send(
        "Rozumiem, že niečo chceš, ale nemám dosť údajov. "
        "Skús to napísať konkrétnejšie: typ tréningu, deň, čas alebo ID tréningu."
    )
    return False


def _has_multiple_plan_options(parsed: dict) -> bool:
    summary = parsed.get("raw_summary", "").casefold()
    return "viac" in summary and any(
        word in summary for word in ("možností", "možné", "tréningov", "plan")
    )


def _has_workout_description(message_text: str, parsed: dict) -> bool:
    if parsed.get("workout_type") or parsed.get("day") or parsed.get("time"):
        return True

    normalized_text = message_text.casefold()
    descriptive_words = (
        "tréning",
        "beh",
        "posilk",
        "dnes",
        "zajtra",
        "pondel",
        "utor",
        "stred",
        "štvrt",
        "stvrt",
        "piatok",
        "sobot",
        "nedeľ",
        "nedel",
    )
    return any(word in normalized_text for word in descriptive_words)


def _get_missing_fields(parsed: dict) -> list[str]:
    required_fields = {
        "plan_workout": ("workout_type", "day", "time"),
        "log_done": ("plan_id", "result_text"),
        "log_short": ("plan_id", "result_text"),
        "log_missed": ("plan_id",),
        "use_joker": ("plan_id", "day", "time"),
        "set_commitment": ("workout_type", "count_per_week"),
    }
    fields = required_fields.get(parsed["intent"], ())
    return [field for field in fields if parsed.get(field) is None]


def _pending_question(intent: str, missing_fields: list[str]) -> str:
    if intent == "use_joker":
        if "plan_id" in missing_fields:
            return "Ktorý tréning chceš posunúť? Napíš ID z jonas my week."
        return "Na ktorý deň a čas to chceš posunúť?"

    if intent == "log_done":
        if "plan_id" in missing_fields:
            return "Ktorý tréning mám označiť ako hotový? Napíš ID z jonas my week."
        return "Aký bol výsledok tréningu?"

    if intent == "log_short":
        if "plan_id" in missing_fields:
            return "Ktorý tréning mám označiť ako skrátený? Napíš ID z jonas my week."
        return "Aký bol výsledok skráteného tréningu?"

    if intent == "log_missed":
        return "Ktorý tréning mám označiť ako vynechaný? Napíš ID z jonas my week."

    if intent == "plan_workout":
        if "workout_type" in missing_fields:
            return "Aký typ tréningu chceš naplánovať?"
        return "Potrebujem deň aj čas. Napíš napríklad: v piatok o 18:00."

    if intent == "set_commitment":
        return "Potrebujem typ tréningu aj počet za týždeň."

    return "Doplň, prosím, chýbajúce údaje."


def _should_resolve_pending(pending_action: dict | None, parsed: dict) -> bool:
    if pending_action is None or pending_action["intent"] != parsed["intent"]:
        return False

    original = pending_action["parsed_json"]
    for field in ("plan_id", "workout_type"):
        old_value = original.get(field)
        new_value = parsed.get(field)
        if old_value is not None and new_value is not None and old_value != new_value:
            return False

    return not _get_missing_fields(parsed)


@client.event
async def on_ready() -> None:
    print(f"Jonáš je online ako {client.user}")
    start_scheduler(client)


@client.event
async def on_message(message: discord.Message) -> None:
    # Bot ignoruje vlastné správy.
    if message.author == client.user:
        return

    discord_user_id = str(message.author.id)
    command_text = _extract_command_text(message)
    is_pending_follow_up = command_text is None
    pending_action = get_latest_pending_action(discord_user_id)
    if command_text is None:
        if pending_action is None:
            return
        command_text = message.content.strip()

    ai_context = build_ai_context(discord_user_id)
    pending_context = build_pending_context(pending_action)
    save_user_message(discord_user_id, message.content)

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
            "jonas stats all, jonas report all, jonas test scheduler. "
            "Debug: jonas pending. "
            "Automatické správy: nedeľa 19:00 plánovanie, denne 06:00 ranný plán, "
            "denne 20:00 príprava na zajtra, po tréningu kontrola. "
            "Prirodzený jazyk: jonas v piatok o 18:00 beh; "
            "jonas tréning 3 hotový, 5.2 km za 32 min; "
            "jonas posuň tréning 4 na sobotu 10:00; jonas ukáž štatistiky."
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

    if normalized_command == "test scheduler":
        _, response = await send_scheduler_test_messages(client)
        await message.channel.send(response)
        return

    if normalized_command == "pending":
        await message.channel.send(format_pending_action(pending_action))
        return

    if normalized_command == "ai test" or normalized_command.startswith("ai test "):
        test_text = command_text[len("ai test") :].strip()
        if not test_text:
            await message.channel.send(
                "Napíš text na testovanie, napríklad: "
                "jonas ai test v piatok o šiestej večer beh"
            )
            return

        author_name = getattr(message.author, "display_name", str(message.author))
        try:
            parsed = await _parse_with_ai(
                test_text, author_name, ai_context, pending_context
            )
        except Exception:
            await message.channel.send(
                "OpenAI parser teraz neodpovedal. Skús to znova o chvíľu."
            )
            return

        if parsed is None:
            await message.channel.send(AI_NOT_CONFIGURED_MESSAGE)
            return

        parser_json = json.dumps(parsed, ensure_ascii=False, indent=2)
        await message.channel.send(f"```json\n{parser_json[:1850]}\n```")
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

    author_name = getattr(message.author, "display_name", str(message.author))
    try:
        parsed = await _parse_with_ai(
            command_text, author_name, ai_context, pending_context
        )
    except Exception:
        await message.channel.send(
            "OpenAI parser teraz neodpovedal. Tvrdé príkazy stále fungujú cez: "
            "jonas help"
        )
        return

    if parsed is None:
        await message.channel.send(AI_NOT_CONFIGURED_MESSAGE)
        return

    if (
        is_pending_follow_up
        and pending_action is not None
        and parsed["intent"] != pending_action["intent"]
    ):
        return

    missing_fields = _get_missing_fields(parsed)
    if missing_fields:
        create_pending_action(
            discord_user_id,
            parsed["intent"],
            command_text,
            missing_fields,
            parsed,
        )
        await message.channel.send(_pending_question(parsed["intent"], missing_fields))
        return

    should_resolve_pending = _should_resolve_pending(pending_action, parsed)
    success = await _execute_ai_intent(message, parsed)
    if success and should_resolve_pending:
        resolve_pending_action(pending_action["id"])


def run_bot() -> None:
    client.run(DISCORD_TOKEN)
