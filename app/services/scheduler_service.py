import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import BOT_TIMEZONE, DISCORD_CHANNEL_ID
from app.database import get_connection
from app.services.coach_responder import generate_final_reply
from app.services.planning_service import DAY_ORDER


_scheduler_task: asyncio.Task | None = None


def start_scheduler(client) -> asyncio.Task | None:
    """Spustí scheduler iba raz, aj keď sa Discord klient znovu pripojí."""
    global _scheduler_task

    if not DISCORD_CHANNEL_ID:
        print("Scheduler sa nespustil: chýba DISCORD_CHANNEL_ID v súbore .env.")
        return None

    if _scheduler_task is not None and not _scheduler_task.done():
        return _scheduler_task

    try:
        int(DISCORD_CHANNEL_ID)
    except ValueError:
        print("Scheduler sa nespustil: DISCORD_CHANNEL_ID musí byť číslo.")
        return None

    _scheduler_task = asyncio.create_task(scheduler_loop(client))
    print(f"Scheduler spustený pre kanál {DISCORD_CHANNEL_ID}")
    return _scheduler_task


async def scheduler_loop(client) -> None:
    """Každú minútu skontroluje, či treba poslať automatickú správu."""
    bot_timezone = get_bot_timezone()

    while not client.is_closed():
        now = datetime.now(bot_timezone)
        try:
            if now.weekday() == 6 and now.hour == 19 and now.minute == 0:
                await send_sunday_planning_message(client, now)
            if now.hour == 6 and now.minute == 0:
                await send_daily_morning_message(client, now)
            if now.hour == 20 and now.minute == 0:
                await send_evening_preparation_message(client, now)
            await send_workout_upcoming_reminders(client, now)
            if 6 <= now.hour < 22 and now.minute == 0:
                await send_post_workout_checks(client, now)
            if now.hour == 5 and now.minute == 59:
                await send_unanswered_reminders(client, now)
        except Exception as error:
            print(f"Scheduler chyba: {error}")

        await asyncio.sleep(60)


async def send_sunday_planning_message(client, now: datetime) -> None:
    key = f"sunday_planning_{now.date().isoformat()}"
    if was_notification_sent(key):
        return
    await _send_once(client, key, _build_sunday_planning_message())


async def send_daily_morning_message(client, now: datetime) -> None:
    key = f"morning_{now.date().isoformat()}"
    if was_notification_sent(key):
        return
    await _send_once(client, key, _build_morning_message(now.date()))


async def send_evening_preparation_message(client, now: datetime) -> None:
    key = f"evening_prep_{now.date().isoformat()}"
    if was_notification_sent(key):
        return

    message = _build_evening_message(now.date() + timedelta(days=1))
    if message is not None:
        await _send_once(client, key, message)


async def send_workout_upcoming_reminders(client, now: datetime) -> None:
    """Presne 15 minút pred začiatkom pripomenie naplánovaný tréning."""
    channel = await get_channel(client)
    if channel is None:
        return

    for plan in _get_plans_starting_in_15_minutes(now):
        key = f"workout_upcoming_plan_{plan['id']}"
        if was_notification_sent(key):
            continue

        factual_message = (
            f"{plan['display_name']}, {_planned_workout_form(plan['display_name'])} "
            f"{plan['workout_type']} o {plan['planned_time']}. "
            "Začínaš o 15 minút, priprav sa."
        )
        await channel.send(await _scheduled_reply(factual_message, "strict"))
        mark_notification_sent(key)


async def send_post_workout_checks(client, now: datetime) -> None:
    channel = await get_channel(client)
    if channel is None:
        return

    for plan in _get_plans_due_for_check(now):
        key = f"post_check_plan_{plan['id']}"
        if was_notification_sent(key):
            continue

        factual_message = (
            f"{plan['display_name']}, {_had_workout_form(plan['display_name'])} tréning. "
            f"Splnené? Zapíš výsledok napríklad: jonas done {plan['plan_ref']} <výsledok>, "
            f"alebo ak to nevyšlo: jonas missed {plan['plan_ref']}"
        )
        await channel.send(await _scheduled_reply(factual_message, "coach"))
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE weekly_plans
                SET status = 'unanswered'
                WHERE id = ? AND status IN ('planned', 'postponed')
                """,
                (plan["id"],),
            )
        mark_notification_sent(key)


async def send_unanswered_reminders(client, now: datetime) -> None:
    channel = await get_channel(client)
    if channel is None:
        return

    previous_day = now.date() - timedelta(days=1)
    for plan in get_unanswered_from_previous_day(previous_day):
        key = f"unanswered_reminder_plan_{plan['id']}_{now.date().isoformat()}"
        if was_notification_sent(key):
            continue

        factual_message = (
            f"{plan['display_name']}, včera zostal tréning nezodpovedaný. "
            "Buď zapíš výsledok, alebo ho označ ako vynechaný. "
            "Ticho nie je stratégia."
        )
        await channel.send(await _scheduled_reply(factual_message, "strict"))
        mark_notification_sent(key)


async def send_scheduler_test_messages(client) -> tuple[bool, str]:
    """Pošle ukážky troch časovaných správ bez zápisu do notification_log."""
    channel = await get_channel(client)
    if channel is None:
        return False, "Test schedulera zlyhal: nastavený Discord kanál nie je dostupný."

    bot_timezone = get_bot_timezone()
    today = datetime.now(bot_timezone).date()
    await channel.send("[TEST] " + _build_morning_message(today))

    evening_message = _build_evening_message(today + timedelta(days=1))
    if evening_message is None:
        evening_message = (
            "Zajtra máš tréning. Nachystaj si veci už večer. "
            "Ráno nechceme debatný krúžok s lenivosťou. "
            "(Testovacia ukážka, zajtra zatiaľ nie je tréning v pláne.)"
        )
    await channel.send("[TEST] " + evening_message)
    await channel.send("[TEST] " + _build_sunday_planning_message())

    return True, "Test schedulera odoslal rannú, večernú a nedeľnú ukážku."


def was_notification_sent(key: str) -> bool:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT id FROM notification_log WHERE notification_key = ?",
            (key,),
        ).fetchone()
    return row is not None


def mark_notification_sent(key: str) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO notification_log (notification_key, sent_at)
            VALUES (?, ?)
            """,
            (key, datetime.now(timezone.utc).isoformat()),
        )


def get_bot_timezone():
    """Vráti nastavené pásmo alebo lokálny Windows čas ako praktický fallback."""
    try:
        return ZoneInfo(BOT_TIMEZONE)
    except ZoneInfoNotFoundError:
        local_timezone = datetime.now().astimezone().tzinfo
        print(
            f"Časové pásmo {BOT_TIMEZONE} nie je dostupné. "
            f"Scheduler používa lokálny čas počítača: {local_timezone}."
        )
        return local_timezone


async def get_channel(client):
    if not DISCORD_CHANNEL_ID:
        return None

    try:
        channel_id = int(DISCORD_CHANNEL_ID)
    except ValueError:
        print("Scheduler nevie načítať kanál: DISCORD_CHANNEL_ID musí byť číslo.")
        return None
    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel

    try:
        return await client.fetch_channel(channel_id)
    except Exception as error:
        print(f"Scheduler nevie načítať kanál {channel_id}: {error}")
        return None


def list_active_users() -> list[dict]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, display_name
            FROM users
            WHERE is_active = 1
            ORDER BY display_name ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_plans_for_date(target_date: date) -> list[dict]:
    """Vráti aktívne tréningy naplánované na konkrétny dátum."""
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT
                weekly_plans.id,
                weekly_plans.user_id,
                weekly_plans.week_start,
                weekly_plans.planned_day,
                weekly_plans.planned_time,
                weekly_plans.workout_type,
                weekly_plans.status,
                users.display_name
            FROM weekly_plans
            JOIN users ON users.id = weekly_plans.user_id
            WHERE users.is_active = 1
              AND weekly_plans.status IN ('planned', 'postponed', 'unanswered')
            ORDER BY weekly_plans.planned_time ASC
            """
        ).fetchall()

    plans = []
    for row in rows:
        if get_plan_date(row["week_start"], row["planned_day"]) != target_date:
            continue
        plan = dict(row)
        plan["plan_ref"] = _get_plan_reference(
            plan["user_id"], plan["week_start"], plan["id"]
        )
        plans.append(plan)
    return plans


def get_unanswered_from_previous_day(target_date: date) -> list[dict]:
    return [
        plan
        for plan in get_plans_for_date(target_date)
        if plan["status"] == "unanswered"
    ]


def get_plan_date(week_start: str, planned_day: str) -> date | None:
    """Určí dátum tréningu z pondelka týždňa a slovenského názvu dňa."""
    day_number = DAY_ORDER.get(planned_day)
    if day_number is None:
        return None

    try:
        monday = date.fromisoformat(week_start)
    except ValueError:
        return None

    return monday + timedelta(days=day_number - 1)


def _build_sunday_planning_message() -> str:
    lines = [
        "Je nedeľa. Plánujeme týždeň. Počet tréningov dnes "
        "nevyjednávame, iba ich dávame do kalendára.",
        "",
        "Aktívne záväzky:",
    ]
    with get_connection() as connection:
        for user in list_active_users():
            commitments = connection.execute(
                """
                SELECT workout_type, count_per_week
                FROM commitments
                WHERE user_id = ? AND is_active = 1
                ORDER BY workout_type ASC
                """,
                (user["id"],),
            ).fetchall()
            summary = ", ".join(
                f"{item['workout_type']} {item['count_per_week']}x"
                for item in commitments
            )
            lines.append(f"- {user['display_name']}: {summary or 'bez záväzkov'}")
    return "\n".join(lines)


def _build_morning_message(target_date: date) -> str:
    plans = get_plans_for_date(target_date)
    if not plans:
        return "Dobré ráno. Dnes nie je naplánovaný žiadny tréning."

    lines = ["Dobré ráno. Dnešné tréningy:"]
    for plan in plans:
        lines.append(
            f"- [{plan['plan_ref']}] {plan['display_name']}: "
            f"{plan['workout_type']} o {plan['planned_time']}. "
            f"{_personal_motivation(plan['display_name'])}"
        )
    return "\n".join(lines)


def _build_evening_message(target_date: date) -> str | None:
    plans = get_plans_for_date(target_date)
    if not plans:
        return None

    lines = [
        "Zajtra máš tréning. Nachystaj si veci už večer. "
        "Ráno nechceme debatný krúžok s lenivosťou."
    ]
    for plan in plans:
        lines.append(
            f"- [{plan['plan_ref']}] {plan['display_name']}: "
            f"{plan['workout_type']} o {plan['planned_time']}"
        )
    return "\n".join(lines)


def _get_plans_due_for_check(now: datetime) -> list[dict]:
    """Pri hodinovej kontrole vráti tréningy začaté pred 60 až 119 minútami."""
    due_plans = []
    for plan in get_plans_for_date(now.date()):
        if plan["status"] not in {"planned", "postponed"}:
            continue

        hours, minutes = (int(part) for part in plan["planned_time"].split(":"))
        planned_at = datetime.combine(
            now.date(),
            datetime.min.time().replace(hour=hours, minute=minutes),
            tzinfo=now.tzinfo,
        )
        elapsed_minutes = (now - planned_at).total_seconds() / 60
        if 60 <= elapsed_minutes < 120:
            due_plans.append(plan)
    return due_plans


def _get_plans_starting_in_15_minutes(now: datetime) -> list[dict]:
    """Vráti planned/postponed tréningy začínajúce presne o 15 minút."""
    target = now + timedelta(minutes=15)
    target_time = target.strftime("%H:%M")
    return [
        plan
        for plan in get_plans_for_date(target.date())
        if plan["status"] in {"planned", "postponed"}
        and plan["planned_time"] == target_time
    ]


def _get_plan_reference(user_id: int, week_start: str, plan_id: int) -> int:
    """Vráti lokálne číslo tréningu v používateľovom týždni."""
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, planned_day, planned_time
            FROM weekly_plans
            WHERE user_id = ? AND week_start = ?
            """,
            (user_id, week_start),
        ).fetchall()
    rows = sorted(
        rows,
        key=lambda row: (
            DAY_ORDER.get(row["planned_day"], 99),
            row["planned_time"],
            row["id"],
        ),
    )
    for index, row in enumerate(rows, start=1):
        if row["id"] == plan_id:
            return index
    return plan_id


async def _send_once(client, key: str, message: str) -> None:
    channel = await get_channel(client)
    if channel is None:
        return
    await channel.send(message)
    mark_notification_sent(key)


async def _scheduled_reply(factual_message: str, tone: str) -> str:
    """Vytvorí variabilnú trénerovskú pripomienku s bezpečným fallbackom."""
    return await asyncio.to_thread(
        generate_final_reply,
        "Automatická pripomienka",
        factual_message,
        "scheduled_reminder",
        tone,
        None,
    )


def _personal_motivation(display_name: str) -> str:
    if display_name.strip().casefold() == "ema":
        return (
            "Dnes je tréningový deň. Stačí začať. "
            "Nemusíš byť motivovaná, stačí byť obutá."
        )
    return (
        "Dnes je tréningový deň. Stačí začať. "
        "Nemusíš byť motivovaný, stačí byť obutý."
    )


def _had_workout_form(display_name: str) -> str:
    if display_name.strip().casefold() == "ema":
        return "mala si"
    return "mal si"


def _planned_workout_form(display_name: str) -> str:
    if display_name.strip().casefold() == "ema":
        return "naplánovala si si"
    return "naplánoval si si"
