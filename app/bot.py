import asyncio
import logging
import re

import discord

from app.ai_agent import AI_NOT_CONFIGURED_MESSAGE, OpenAIKeyMissingError, decide_agent_action
from app.config import ADMIN_DISCORD_USER_ID, DISCORD_CHANNEL_ID, DISCORD_TOKEN
from app.database import get_connection
from app.services.coach_responder import (
    generate_final_reply,
    generate_coach_reply,
    respond_error,
    respond_forbidden_walk,
    respond_joker,
    respond_missed,
    respond_planning_success,
    respond_shortened,
    respond_stats,
    respond_success,
)
from app.services.capabilities_service import get_help
from app.services.commitments_service import list_commitments
from app.services.commitment_change_service import (
    list_changes,
    request_commitment_change,
    vote_change,
)
from app.services.context_service import build_ai_context, build_debug_context, save_channel_message
from app.services.dev_reset_service import reset_all, reset_me, reset_user
from app.services.joker_service import JOKER_FORMAT_MESSAGE, joker_status, use_joker
from app.services.onboarding_service import (
    confirm_onboarding,
    get_onboarding_status,
    has_active_onboarding,
    reset_onboarding,
    start_onboarding,
)
from app.services.pending_actions_service import (
    build_pending_context,
    clear_old_pending_actions,
    get_latest_pending_action,
)
from app.tool_executor import execute_tool
from app.services.planning_service import (
    PLAN_FORMAT_MESSAGE,
    add_plan,
    list_all_week,
    list_my_week,
    resolve_plan_reference,
    weekly_status,
)
from app.services.scheduler_service import send_scheduler_test_messages, start_scheduler
from app.services.replacement_service import (
    approve_replacement,
    get_replacement_detail,
    list_replacements,
    reject_replacement,
    request_workout_replacement,
)
from app.services.stats_service import (
    MONTH_FORMAT_MESSAGE,
    get_all_month_stats,
    get_user_month_stats,
)
from app.services.users_service import ensure_user_exists, list_users
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
intents.members = True

client = discord.Client(intents=intents)
logger = logging.getLogger(__name__)


async def send_and_remember(channel, content: str):
    """Send a Jonáš response and persist it in channel memory."""
    content = str(content)
    internal_markers = (
        "request_id",
        "week_planning",
        "plan_slots",
        "missing args",
        "pending action",
        "ai agent failed",
    )
    if any(marker in content.casefold() for marker in internal_markers):
        logger.warning("Outgoing internal detail suppressed")
        content = "Prepáč, toto som nezachytil správne. Skús mi to napísať ešte raz jednoduchšie."
    sent = await channel.send(content)
    bot_user_id = str(client.user.id) if client.user else "jonas"
    bot_name = getattr(client.user, "display_name", "Jonáš") if client.user else "Jonáš"
    save_channel_message(bot_user_id, bot_name, str(channel.id), content, is_bot=True)
    return sent


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


def _parse_replacement_request(command_text: str) -> tuple[int, str, str, str, str] | None:
    prefix = "replacement request "
    if not command_text.casefold().startswith(prefix):
        return None
    parts = command_text[len(prefix) :].split(maxsplit=4)
    if len(parts) != 5 or not parts[0].isdigit():
        return None
    plan_ref, workout_type, day, time, reason = parts
    return int(plan_ref), workout_type, day, time, reason


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


def _coach_service_response(success: bool, response: str, kind: str) -> str:
    if not success:
        normalized_response = response.casefold()
        if "prech" in normalized_response:
            return respond_forbidden_walk()
        return respond_error(response)

    responders = {
        "success": respond_success,
        "shortened": respond_shortened,
        "missed": respond_missed,
        "joker": respond_joker,
        "planning": respond_planning_success,
        "stats": respond_stats,
    }
    return responders[kind](response)


def _coach_stats_response(response: str) -> str:
    if response == MONTH_FORMAT_MESSAGE:
        return respond_error(response)
    return respond_stats(response)


async def _natural_coach_response(
    message: discord.Message,
    event_type: str,
    factual_result: str,
    success: bool = True,
) -> None:
    if not success:
        event_type = "forbidden_walk" if "prech" in factual_result.casefold() else "error"

    user_context = f"Používateľ: {getattr(message.author, 'display_name', message.author)}"
    response = await asyncio.to_thread(
        generate_coach_reply, event_type, factual_result, user_context
    )
    await send_and_remember(message.channel, response)


def _is_admin(discord_user_id: str) -> tuple[bool, str | None]:
    if not ADMIN_DISCORD_USER_ID:
        return False, "ADMIN_DISCORD_USER_ID nie je nastavené. Dev reset je vypnutý."
    if discord_user_id != ADMIN_DISCORD_USER_ID.strip():
        return False, "Tento príkaz môže použiť iba admin."
    return True, None


def _natural_approval_result(
    discord_user_id: str, command_text: str
) -> tuple[bool, str, str] | None:
    normalized = command_text.strip().casefold()
    approvals = {"schvaľujem", "schvalujem", "súhlasím", "suhlasim", "ok", "áno", "ano"}
    rejections = {"neschvaľujem", "neschvalujem", "nesúhlasím", "nesuhlasim", "nie"}
    if normalized not in approvals | rejections:
        return None
    vote = "approve" if normalized in approvals else "reject"
    with get_connection() as connection:
        changes = connection.execute(
            """
            SELECT requests.id
            FROM commitment_change_requests requests
            WHERE requests.status = 'open'
              AND NOT EXISTS (
                  SELECT 1 FROM commitment_change_votes votes
                  WHERE votes.request_id = requests.id
                    AND votes.voter_discord_user_id = ?
              )
            """,
            (discord_user_id,),
        ).fetchall()
        replacements = connection.execute(
            """
            SELECT requests.id
            FROM workout_replacement_requests requests
            WHERE requests.status = 'open'
              AND NOT EXISTS (
                  SELECT 1 FROM workout_replacement_votes votes
                  WHERE votes.request_id = requests.id
                    AND votes.voter_discord_user_id = ?
              )
            """,
            (discord_user_id,),
        ).fetchall()
    candidates = [("change", row["id"]) for row in changes] + [
        ("replacement", row["id"]) for row in replacements
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        return (
            False,
            "Neviem jednoznačne, ktorý návrh chceš schváliť. Napíš číslo návrhu "
            "alebo mi povedz, čo presne schvaľuješ.",
            "clarify",
        )
    kind, request_number = candidates[0]
    if kind == "change":
        success, result = vote_change(discord_user_id, request_number, vote)
    else:
        function = approve_replacement if vote == "approve" else reject_replacement
        success, result = function(discord_user_id, request_number)
    return success, result, "planning" if kind == "change" else "system_info"


async def _decide_with_agent(
    message_text: str,
    author_display_name: str,
    context_text: str,
    pending_action_text: str,
) -> dict | None:
    try:
        return await asyncio.to_thread(
            decide_agent_action,
            message_text,
            author_display_name,
            context_text,
            pending_action_text,
        )
    except OpenAIKeyMissingError:
        return None
    except Exception:
        logger.exception("AI agent call failed")
        raise


async def _send_agent_result(
    message: discord.Message,
    original_message: str,
    factual_result: str,
    result_type: str,
    tone: str,
    ai_context: str,
) -> None:
    if result_type in {"user_error", "clarify"}:
        await send_and_remember(message.channel, factual_result)
        return

    reply = await asyncio.to_thread(
        generate_final_reply,
        original_message,
        factual_result,
        result_type,
        tone,
        ai_context[:1800],
    )
    await send_and_remember(message.channel, reply)


def _resolve_plan_id(discord_user_id: str, plan_ref: int) -> tuple[bool, int | str]:
    return resolve_plan_reference(discord_user_id, plan_ref)


def _request_or_set_commitment(
    discord_user_id: str, workout_type: str, count_per_week: int
) -> tuple[bool, str]:
    """Nový záväzok nastaví priamo, zmenu existujúceho pošle na hlasovanie."""
    return request_commitment_change(discord_user_id, workout_type, count_per_week)


@client.event
async def on_ready() -> None:
    logger.info("Jonáš je online ako %s", client.user)
    start_scheduler(client)


async def _send_onboarding_welcome(channel, discord_user_id: str, display_name: str) -> None:
    start_onboarding(discord_user_id)
    factual = (
        f"<@{discord_user_id}> vitaj v Couple GlowUp. Jonáš ti pomôže držať weekly "
        "commitments, plánovať tréningy a zapisovať výsledky. Napíš jednou vetou, "
        "aké aktivity chceš robiť a koľkokrát týždenne. Pri nových aktivitách sa potom "
        "spýtam, aké výsledky chceš sledovať."
    )
    reply = await asyncio.to_thread(
        generate_final_reply,
        "Automatické privítanie nového používateľa",
        factual,
        "scheduled_reminder",
        "supportive",
        f"Používateľ: {display_name}",
    )
    await send_and_remember(channel, reply)


@client.event
async def on_member_join(member: discord.Member) -> None:
    if getattr(member, "bot", False):
        return
    created, _ = ensure_user_exists(str(member.id), member.display_name)
    if not created or not DISCORD_CHANNEL_ID:
        return
    try:
        channel = client.get_channel(int(DISCORD_CHANNEL_ID))
    except ValueError:
        return
    if channel is None:
        try:
            channel = await client.fetch_channel(int(DISCORD_CHANNEL_ID))
        except Exception:
            return
    if channel is not None:
        await _send_onboarding_welcome(channel, str(member.id), member.display_name)


@client.event
async def on_message(message: discord.Message) -> None:
    # Bot ignoruje vlastné správy.
    if getattr(message.author, "bot", False):
        return

    discord_user_id = str(message.author.id)
    channel_id = str(message.channel.id)
    author_name = getattr(message.author, "display_name", str(message.author))
    command_text = _extract_command_text(message)
    activated = command_text is not None
    logger.debug(
        "Message received channel=%s author=%s content=%r activated=%s",
        channel_id,
        discord_user_id,
        message.content,
        activated,
    )
    created, _ = ensure_user_exists(discord_user_id, author_name)
    save_channel_message(discord_user_id, author_name, channel_id, message.content)
    commitments = list_commitments(discord_user_id)
    logger.debug(
        "Ensure user user=%s state=%s has_commitments=%s",
        discord_user_id,
        "new" if created else "existed",
        bool(commitments),
    )
    if created:
        await _send_onboarding_welcome(message.channel, discord_user_id, author_name)

    pending_action = get_latest_pending_action(discord_user_id)
    active_onboarding = has_active_onboarding(discord_user_id)
    natural_approval = None
    if command_text is None:
        natural_approval = _natural_approval_result(discord_user_id, message.content)
        if not active_onboarding and natural_approval is None:
            return
        command_text = message.content.strip()
    ai_context = build_ai_context(discord_user_id, channel_id, 5)
    pending_context = build_pending_context(pending_action)
    logger.debug(
        "Message context user=%s pending=%s commitments=%s",
        discord_user_id,
        pending_action["intent"] if pending_action else None,
        len(commitments),
    )

    normalized_command = command_text.casefold()

    if normalized_command == "debug context":
        is_admin, error = _is_admin(discord_user_id)
        await send_and_remember(
            message.channel,
            build_debug_context(discord_user_id, channel_id) if is_admin else error,
        )
        return

    natural_approval = natural_approval or _natural_approval_result(
        discord_user_id, command_text
    )
    if natural_approval is not None:
        success, factual_result, result_type = natural_approval
        await _send_agent_result(
            message,
            command_text,
            factual_result,
            result_type if success else "user_error",
            "neutral",
            ai_context,
        )
        return

    if normalized_command == "help":
        await send_and_remember(message.channel, get_help())
        return

    if normalized_command == "ping":
        await send_and_remember(message.channel, "Som online. Žiadne výhovorky.")
        return

    if normalized_command.startswith("dev reset"):
        is_admin, error = _is_admin(discord_user_id)
        if not is_admin:
            await send_and_remember(message.channel, respond_error(error))
            return

        if normalized_command == "dev reset me":
            success, response = reset_me(discord_user_id)
        elif normalized_command == "dev reset all":
            success, response = reset_all()
        elif normalized_command.startswith("dev reset user "):
            display_name = command_text[len("dev reset user ") :].strip()
            success, response = reset_user(display_name)
        else:
            success = False
            response = (
                "Formát: jonas dev reset me, jonas dev reset all alebo "
                "jonas dev reset user <meno>"
            )

        if success:
            await send_and_remember(message.channel,
                f"{response} Reset je hotový. Dáta sú preč. "
                "Používateľ môže začať onboarding odznova."
            )
        else:
            await send_and_remember(message.channel, respond_error(response))
        return

    if normalized_command == "onboarding start":
        success, response = start_onboarding(discord_user_id)
        await send_and_remember(message.channel, response if success else respond_error(response))
        return

    if normalized_command in {"onboarding status", "onboarding debug"}:
        success, response = get_onboarding_status(discord_user_id)
        await send_and_remember(message.channel, response if success else respond_error(response))
        return

    if normalized_command == "onboarding reset":
        success, response = reset_onboarding(discord_user_id)
        await send_and_remember(message.channel, response if success else respond_error(response))
        return

    if normalized_command == "onboarding confirm":
        success, response = confirm_onboarding(discord_user_id)
        await send_and_remember(message.channel,
            _coach_service_response(success, response, "success")
        )
        return

    if normalized_command.startswith("coach test "):
        payload = command_text[len("coach test ") :].strip()
        event_type, _, detail = payload.partition(" ")
        if event_type == "advice":
            event_type = "general_advice"
            factual_result = detail or "Čo je najlepšie na výbušnosť?"
        elif event_type in {"success", "missed"}:
            factual_result = (
                "Testovací tréning bol splnený."
                if event_type == "success"
                else "Testovací tréning bol vynechaný."
            )
        else:
            await send_and_remember(message.channel,
                "Použi: jonas coach test success, jonas coach test missed alebo "
                "jonas coach test advice výbušnosť"
            )
            return

        await _natural_coach_response(message, event_type, factual_result)
        return

    if normalized_command == "users":
        await send_and_remember(message.channel, _format_users())
        return

    if normalized_command == "changes":
        await send_and_remember(message.channel, list_changes())
        return

    if normalized_command == "replacements":
        await send_and_remember(message.channel, list_replacements())
        return

    if normalized_command.startswith("replacement request "):
        parsed_replacement = _parse_replacement_request(command_text)
        if parsed_replacement is None:
            await send_and_remember(message.channel,
                "Použi: jonas replacement request <číslo> <typ> <deň> <čas> <dôvod>"
            )
            return
        plan_ref, workout_type, day, time, reason = parsed_replacement
        success, response = request_workout_replacement(
            discord_user_id, plan_ref, None, workout_type, day, time, reason
        )
        await send_and_remember(message.channel, response)
        return

    if normalized_command.startswith("approve replacement ") or normalized_command.startswith(
        "reject replacement "
    ):
        parts = normalized_command.split()
        if len(parts) != 3 or not parts[2].isdigit():
            await send_and_remember(message.channel,
                "Použi: jonas approve replacement <id> alebo jonas reject replacement <id>"
            )
            return
        function = approve_replacement if parts[0] == "approve" else reject_replacement
        _, response = function(discord_user_id, int(parts[2]))
        await send_and_remember(message.channel, response)
        return

    if normalized_command.startswith("replacement "):
        parts = normalized_command.split()
        if len(parts) == 2 and parts[1].isdigit():
            await send_and_remember(message.channel, get_replacement_detail(int(parts[1])))
        else:
            await send_and_remember(message.channel, "Použi: jonas replacement <id>")
        return

    if normalized_command.startswith("approve change ") or normalized_command.startswith(
        "reject change "
    ):
        parts = normalized_command.split()
        if len(parts) != 3 or not parts[2].isdigit():
            await send_and_remember(message.channel,
                "Použi: jonas approve change <id> alebo jonas reject change <id>"
            )
            return
        vote = "approve" if parts[0] == "approve" else "reject"
        success, response = vote_change(discord_user_id, int(parts[2]), vote)
        await send_and_remember(message.channel, _coach_service_response(success, response, "success"))
        return

    if normalized_command.startswith("commitment "):
        parsed_commitment = _parse_commitment_command(command_text)
        if parsed_commitment is None:
            await send_and_remember(message.channel,
                "Nerozumiem záväzku. Skús napríklad: jonas commitment beh 2"
            )
            return

        workout_type, count_per_week = parsed_commitment
        success, response = _request_or_set_commitment(
            str(message.author.id), workout_type, count_per_week
        )
        await send_and_remember(message.channel, _coach_service_response(success, response, "success"))
        return

    if normalized_command == "commitments":
        await send_and_remember(message.channel, _format_commitments(str(message.author.id)))
        return

    if normalized_command == "commitments all":
        await send_and_remember(message.channel, _format_commitments())
        return

    if normalized_command == "planning status":
        success, response = weekly_status(str(message.author.id))
        await send_and_remember(message.channel,
            respond_stats(response) if success else respond_error(response)
        )
        return

    if normalized_command == "plan" or normalized_command.startswith("plan "):
        parsed_plan = _parse_plan_command(command_text)
        if parsed_plan is None:
            await send_and_remember(message.channel, PLAN_FORMAT_MESSAGE)
            return

        workout_type, planned_day, planned_time = parsed_plan
        success, response = add_plan(
            str(message.author.id), workout_type, planned_day, planned_time
        )
        await send_and_remember(message.channel, _coach_service_response(success, response, "planning"))
        return

    if normalized_command == "my week":
        success, response = list_my_week(str(message.author.id))
        await send_and_remember(message.channel,
            respond_stats(response) if success else respond_error(response)
        )
        return

    if normalized_command == "week":
        await send_and_remember(message.channel, respond_stats(list_all_week()))
        return

    if normalized_command == "joker status":
        success, response = joker_status(str(message.author.id))
        await send_and_remember(message.channel,
            respond_stats(response) if success else respond_error(response)
        )
        return

    if normalized_command == "joker" or normalized_command.startswith("joker "):
        parsed_joker = _parse_joker_command(command_text)
        if parsed_joker is None:
            await send_and_remember(message.channel, JOKER_FORMAT_MESSAGE)
            return

        plan_id, new_day, new_time = parsed_joker
        resolved, plan_id_or_message = _resolve_plan_id(discord_user_id, plan_id)
        if not resolved:
            await send_and_remember(message.channel, respond_error(str(plan_id_or_message)))
            return
        success, response = use_joker(
            str(message.author.id), int(plan_id_or_message), new_day, new_time
        )
        await send_and_remember(message.channel, _coach_service_response(success, response, "joker"))
        return

    if normalized_command == "test scheduler":
        success, response = await send_scheduler_test_messages(client)
        await send_and_remember(message.channel, _coach_service_response(success, response, "success"))
        return

    if normalized_command.startswith("stats") or normalized_command.startswith(
        "report"
    ):
        parsed_stats = _parse_stats_command(command_text)
        if parsed_stats is None:
            await send_and_remember(message.channel, MONTH_FORMAT_MESSAGE)
            return

        show_all, month = parsed_stats
        if show_all:
            await send_and_remember(message.channel, _coach_stats_response(get_all_month_stats(month)))
        else:
            success, response = get_user_month_stats(str(message.author.id), month)
            await send_and_remember(message.channel,
                _coach_stats_response(response) if success else respond_error(response)
            )
        return

    if normalized_command == "done" or normalized_command.startswith("done "):
        parsed_result = _parse_result_command(command_text, "done")
        if parsed_result is None:
            await send_and_remember(message.channel, DONE_FORMAT_MESSAGE)
            return

        plan_id, result_text = parsed_result
        resolved, plan_id_or_message = _resolve_plan_id(discord_user_id, plan_id)
        if not resolved:
            await send_and_remember(message.channel, respond_error(str(plan_id_or_message)))
            return
        success, response = complete_workout(
            str(message.author.id), int(plan_id_or_message), result_text
        )
        await send_and_remember(message.channel, _coach_service_response(success, response, "success"))
        return

    if normalized_command == "short" or normalized_command.startswith("short "):
        parsed_result = _parse_result_command(command_text, "short")
        if parsed_result is None:
            await send_and_remember(message.channel, SHORT_FORMAT_MESSAGE)
            return

        plan_id, result_text = parsed_result
        resolved, plan_id_or_message = _resolve_plan_id(discord_user_id, plan_id)
        if not resolved:
            await send_and_remember(message.channel, respond_error(str(plan_id_or_message)))
            return
        success, response = shorten_workout(
            str(message.author.id), int(plan_id_or_message), result_text
        )
        await send_and_remember(message.channel,
            _coach_service_response(success, response, "shortened")
        )
        return

    if normalized_command == "missed" or normalized_command.startswith("missed "):
        plan_id = _parse_plan_id_command(command_text, "missed")
        if plan_id is None:
            await send_and_remember(message.channel, MISSED_FORMAT_MESSAGE)
            return

        resolved, plan_id_or_message = _resolve_plan_id(discord_user_id, plan_id)
        if not resolved:
            await send_and_remember(message.channel, respond_error(str(plan_id_or_message)))
            return
        success, response = miss_workout(str(message.author.id), int(plan_id_or_message))
        await send_and_remember(message.channel, _coach_service_response(success, response, "missed"))
        return

    if normalized_command == "workout" or normalized_command.startswith("workout "):
        plan_id = _parse_plan_id_command(command_text, "workout")
        if plan_id is None:
            await send_and_remember(message.channel, WORKOUT_FORMAT_MESSAGE)
            return

        resolved, plan_id_or_message = _resolve_plan_id(discord_user_id, plan_id)
        if not resolved:
            await send_and_remember(message.channel, respond_error(str(plan_id_or_message)))
            return
        success, response = get_workout_detail(
            str(message.author.id), int(plan_id_or_message)
        )
        if success:
            await send_and_remember(message.channel, respond_stats(response))
        else:
            await send_and_remember(message.channel, respond_error(response))
        return

    try:
        agent_decision = await _decide_with_agent(
            command_text, author_name, ai_context, pending_context
        )
    except Exception:
        logger.exception("Fallback reason=agent_exception user=%s", discord_user_id)
        await send_and_remember(message.channel,
            "AI agent teraz neodpovedal. Skús to znova o chvíľu."
        )
        return

    if agent_decision is None:
        await send_and_remember(message.channel, AI_NOT_CONFIGURED_MESSAGE)
        return

    if agent_decision["mode"] == "clarify":
        await send_and_remember(message.channel,
            agent_decision["clarification_question"]
            or "Čo presne chceš spraviť?"
        )
        return

    if agent_decision["mode"] == "reply":
        if agent_decision["reply_intent"] == "cancel_pending":
            clear_old_pending_actions(discord_user_id)
            await send_and_remember(message.channel, "Dobre, presun som zrušil. Nič som nezmenil.")
            return
        factual_result = (
            agent_decision["args"].get("answer")
            or agent_decision["args"].get("question")
            or agent_decision["reason_summary"]
        )
        result_type = (
            "casual"
            if agent_decision["tone"] == "casual"
            else "general_advice"
        )
        await _send_agent_result(
            message,
            command_text,
            factual_result,
            result_type,
            agent_decision["tone"],
            ai_context,
        )
        return

    tool_name = agent_decision["tool"]
    if not tool_name:
        await send_and_remember(message.channel, "Neviem bezpečne určiť ďalší krok. Skús to spresniť.")
        return

    success, factual_result, result_type = await asyncio.to_thread(
        execute_tool, tool_name, agent_decision["args"], discord_user_id
    )
    if not success and result_type != "user_error":
        result_type = "user_error"
    await _send_agent_result(
        message,
        command_text,
        factual_result,
        result_type,
        agent_decision["tone"],
        ai_context,
    )


def run_bot() -> None:
    client.run(DISCORD_TOKEN)
