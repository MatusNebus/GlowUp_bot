import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import BOT_TIMEZONE, DISCORD_CHANNEL_ID
from app.database import get_connection
from app.services.activity_service import format_result_prompt
from app.services.coach_responder import generate_final_reply
from app.services.joker_service import use_joker
from app.services.planning_service import DAY_ORDER
from app.services.context_service import save_channel_message
from app.services.workout_service import miss_workout


_scheduler_task: asyncio.Task | None = None
logger = logging.getLogger(__name__)


def start_scheduler(client) -> asyncio.Task | None:
    global _scheduler_task
    if not DISCORD_CHANNEL_ID:
        logger.info("Scheduler sa nespustil: chýba DISCORD_CHANNEL_ID.")
        return None
    try:
        int(DISCORD_CHANNEL_ID)
    except ValueError:
        logger.error("Scheduler sa nespustil: DISCORD_CHANNEL_ID musí byť číslo.")
        return None
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(scheduler_loop(client))
    return _scheduler_task


async def scheduler_loop(client) -> None:
    while not client.is_closed():
        now = datetime.now(get_bot_timezone())
        try:
            if now.minute in {0, 30}:
                await send_commitment_reminders(client, now)
            if now.weekday() == 6 and now.hour == 19 and now.minute == 0:
                await send_sunday_planning_message(client, now)
            if now.hour == 6 and now.minute == 0:
                await send_daily_morning_message(client, now)
            if now.hour == 21 and now.minute == 0:
                await send_evening_preparation_message(client, now)
            await send_workout_upcoming_reminders(client, now)
            await send_post_workout_checks(client, now)
            if now.hour == 5 and now.minute == 59:
                await send_unanswered_reminders(client, now)
            if now.hour == 12 and now.minute == 0:
                await resolve_unanswered_workouts(client, now)
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(60)


async def send_commitment_reminders(client, now: datetime) -> None:
    if not 6 <= now.hour < 22:
        return
    for user in _users_missing_commitments(now):
        key = f"commitments_missing:{user['discord_user_id']}:{now:%Y-%m-%d-%H}"
        facts = (
            f"<@{user['discord_user_id']}> ešte nemáš weekly commitments. "
            "Napíš mi aktivity a počet opakovaní za týždeň, aby sme mohli plánovať."
        )
        if await _send_facts_once(client, key, facts, "coach"):
            with get_connection() as connection:
                connection.execute(
                    "UPDATE users SET last_commitment_reminder_at = ? WHERE id = ?",
                    (now.astimezone(timezone.utc).isoformat(), user["id"]),
                )


async def send_sunday_planning_message(client, now: datetime) -> None:
    next_week = (now.date() + timedelta(days=7 - now.weekday())).isoformat()
    with get_connection() as connection:
        users = connection.execute(
            """
            SELECT users.discord_user_id, users.display_name,
                   GROUP_CONCAT(commitments.workout_type || ' ' ||
                                commitments.count_per_week || 'x', ', ') AS summary
            FROM users
            JOIN commitments ON commitments.user_id = users.id AND commitments.is_active = 1
            WHERE users.is_active = 1
            GROUP BY users.id
            ORDER BY users.id
            """
        ).fetchall()
    if not users:
        return
    facts = (
        "Nedeľná výzva na plánovanie budúceho týždňa od "
        f"{next_week}. "
        + "; ".join(
            f"<@{user['discord_user_id']}>: {user['summary']}" for user in users
        )
    )
    await _send_facts_once(client, f"sunday_planning:{now:%Y-%W}", facts, "coach")


async def send_daily_morning_message(client, now: datetime) -> None:
    for user_id, plans in _group_plans(get_plans_for_date(now.date())).items():
        facts = _personal_plan_facts(user_id, plans, "Dnešné tréningy")
        await _send_facts_once(
            client, f"morning:{user_id}:{now:%Y-%m-%d}", facts, "coach"
        )


async def send_evening_preparation_message(client, now: datetime) -> None:
    tomorrow = now.date() + timedelta(days=1)
    for user_id, plans in _group_plans(get_plans_for_date(tomorrow)).items():
        facts = _personal_plan_facts(user_id, plans, "Zajtrajšie tréningy")
        await _send_facts_once(
            client, f"evening:{user_id}:{tomorrow.isoformat()}", facts, "coach"
        )


async def send_workout_upcoming_reminders(client, now: datetime) -> None:
    for plan in _plans_starting_in_15_minutes(now):
        facts = (
            f"<@{plan['discord_user_id']}> má o 15 minút aktivitu "
            f"{plan['workout_type']} o {plan['planned_time']}."
        )
        await _send_facts_once(client, f"preworkout:{plan['id']}", facts, "strict")


async def send_post_workout_checks(client, now: datetime) -> None:
    if not 6 <= now.hour < 22:
        return
    for plan in get_plans_for_date(now.date()):
        elapsed = _elapsed_minutes(plan, now)
        if plan["status"] in {"planned", "postponed"} and 120 <= elapsed < 180:
            key = f"postworkout_initial:{plan['id']}"
            if await _send_facts_once(client, key, _post_workout_facts(plan), "coach"):
                with get_connection() as connection:
                    connection.execute(
                        """
                        UPDATE weekly_plans SET status = 'unanswered'
                        WHERE id = ? AND status IN ('planned', 'postponed')
                        """,
                        (plan["id"],),
                    )
        elif plan["status"] == "unanswered" and elapsed >= 180 and now.minute == 0:
            key = f"postworkout_hourly:{plan['id']}:{now:%Y-%m-%d-%H}"
            await _send_facts_once(client, key, _post_workout_facts(plan), "strict")


async def send_unanswered_reminders(client, now: datetime) -> None:
    yesterday = now.date() - timedelta(days=1)
    for plan in get_unanswered_from_previous_day(yesterday):
        joker_available = _joker_available(plan["user_id"], plan["week_start"])
        consequence = (
            "Ak neodpovieš do 12:00, môže sa automaticky použiť tvoj žolík."
            if joker_available
            else "Ak neodpovieš do 12:00, tréning bude označený ako missed."
        )
        facts = (
            f"<@{plan['discord_user_id']}> včera neodpovedal k aktivite "
            f"{plan['workout_type']}. Naozaj si ju nesplnil/a? {consequence}"
        )
        await _send_facts_once(client, f"unanswered_0559:{plan['id']}", facts, "strict")


async def resolve_unanswered_workouts(client, now: datetime) -> None:
    yesterday = now.date() - timedelta(days=1)
    for plan in get_unanswered_from_previous_day(yesterday):
        key = f"unanswered_1200:{plan['id']}"
        if was_notification_sent(key):
            continue
        success = False
        result = ""
        if _joker_available(plan["user_id"], plan["week_start"]):
            fallback_time = "20:00"
            if now.strftime("%H:%M") < fallback_time:
                success, result = use_joker(
                    plan["discord_user_id"], plan["id"], _day_name(now.date()), fallback_time
                )
        if not success:
            success, result = miss_workout(plan["discord_user_id"], plan["id"])
        facts = f"<@{plan['discord_user_id']}> {result}"
        await _send_facts_once(client, key, facts, "strict")


async def send_scheduler_test_messages(client) -> tuple[bool, str]:
    now = datetime.now(get_bot_timezone())
    await send_commitment_reminders(client, now.replace(hour=8, minute=0))
    await send_daily_morning_message(client, now.replace(hour=6, minute=0))
    await send_evening_preparation_message(client, now.replace(hour=21, minute=0))
    return True, "Scheduler test spustil commitment, rannú a večernú kontrolu."


def get_plans_for_date(target_date: date) -> list[dict]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT p.*, u.discord_user_id, u.display_name
            FROM weekly_plans p
            JOIN users u ON u.id = p.user_id
            WHERE u.is_active = 1
              AND p.status IN ('planned', 'postponed', 'unanswered')
            ORDER BY p.planned_time, p.id
            """
        ).fetchall()
    return [
        dict(row)
        for row in rows
        if get_plan_date(row["week_start"], row["planned_day"]) == target_date
    ]


def get_unanswered_from_previous_day(target_date: date) -> list[dict]:
    return [plan for plan in get_plans_for_date(target_date) if plan["status"] == "unanswered"]


def get_plan_date(week_start: str, planned_day: str) -> date | None:
    try:
        return date.fromisoformat(week_start) + timedelta(days=DAY_ORDER[planned_day] - 1)
    except (ValueError, KeyError):
        return None


def was_notification_sent(key: str) -> bool:
    with get_connection() as connection:
        return connection.execute(
            "SELECT 1 FROM notification_log WHERE notification_key = ?", (key,)
        ).fetchone() is not None


def mark_notification_sent(key: str) -> None:
    with get_connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO notification_log (notification_key, sent_at) VALUES (?, ?)",
            (key, datetime.now(timezone.utc).isoformat()),
        )


def get_bot_timezone():
    try:
        return ZoneInfo(BOT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().tzinfo


async def get_channel(client):
    if not DISCORD_CHANNEL_ID:
        return None
    channel_id = int(DISCORD_CHANNEL_ID)
    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await client.fetch_channel(channel_id)
    except Exception:
        logger.exception("Scheduler nevie načítať kanál %s", channel_id)
        return None


async def _send_facts_once(client, key: str, facts: str, tone: str) -> bool:
    if was_notification_sent(key):
        return False
    channel = await get_channel(client)
    if channel is None:
        return False
    message = await asyncio.to_thread(
        generate_final_reply,
        "Automatická správa schedulera",
        facts,
        "scheduled_reminder",
        tone,
        None,
    )
    await channel.send(message)
    bot_user = getattr(client, "user", None)
    save_channel_message(
        str(getattr(bot_user, "id", "jonas")),
        getattr(bot_user, "display_name", "Jonáš"),
        str(channel.id),
        message,
        is_bot=True,
    )
    mark_notification_sent(key)
    return True


def _users_missing_commitments(now: datetime) -> list[dict]:
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=2)
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT users.*
            FROM users
            WHERE users.is_active = 1
              AND NOT EXISTS (
                  SELECT 1 FROM commitments
                  WHERE commitments.user_id = users.id AND commitments.is_active = 1
              )
            ORDER BY users.id
            """
        ).fetchall()
    result = []
    for row in rows:
        last = row["last_commitment_reminder_at"]
        if not last or datetime.fromisoformat(last) <= cutoff:
            result.append(dict(row))
    return result


def _group_plans(plans: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for plan in plans:
        grouped.setdefault(plan["discord_user_id"], []).append(plan)
    return grouped


def _personal_plan_facts(user_id: str, plans: list[dict], title: str) -> str:
    summary = ", ".join(f"{plan['workout_type']} o {plan['planned_time']}" for plan in plans)
    return f"<@{user_id}> {title}: {summary}."


def _plans_starting_in_15_minutes(now: datetime) -> list[dict]:
    target = now + timedelta(minutes=15)
    return [
        plan
        for plan in get_plans_for_date(target.date())
        if plan["status"] in {"planned", "postponed"}
        and plan["planned_time"] == target.strftime("%H:%M")
    ]


def _elapsed_minutes(plan: dict, now: datetime) -> float:
    hours, minutes = map(int, plan["planned_time"].split(":"))
    planned_at = datetime.combine(
        get_plan_date(plan["week_start"], plan["planned_day"]),
        datetime.min.time().replace(hour=hours, minute=minutes),
        tzinfo=now.tzinfo,
    )
    return (now - planned_at).total_seconds() / 60


def _post_workout_facts(plan: dict) -> str:
    fields = format_result_prompt(plan["activity_version_id"])
    return (
        f"<@{plan['discord_user_id']}> dokončil/a si aktivitu {plan['workout_type']}? "
        f"Napíš tieto výsledky: {fields}."
    )


def _joker_available(user_id: int, week_start: str) -> bool:
    with get_connection() as connection:
        return connection.execute(
            "SELECT 1 FROM jokers WHERE user_id = ? AND week_start = ?",
            (user_id, week_start),
        ).fetchone() is None


def _day_name(target_date: date) -> str:
    return next(name for name, order in DAY_ORDER.items() if order == target_date.weekday() + 1)
