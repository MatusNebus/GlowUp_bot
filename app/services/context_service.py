from datetime import date, datetime, timedelta, timezone

from app.database import get_connection
from app.services.planning_service import DAY_ORDER, get_current_week_start


def save_user_message(discord_user_id: str, message_text: str) -> None:
    """Uloží správu používateľa, ktorá aktivovala Jonáša."""
    clean_text = message_text.strip()
    if not clean_text:
        return

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO message_memory (discord_user_id, message_text, created_at)
            VALUES (?, ?, ?)
            """,
            (discord_user_id, clean_text, datetime.now(timezone.utc).isoformat()),
        )


def build_ai_context(discord_user_id: str, message_limit: int = 5) -> str:
    """Pripraví stručný databázový kontext pre AI parser."""
    today = date.today()
    tomorrow = today + timedelta(days=1)
    week_start = get_current_week_start()

    with get_connection() as connection:
        user = connection.execute(
            """
            SELECT id, display_name
            FROM users
            WHERE discord_user_id = ? AND is_active = 1
            """,
            (discord_user_id,),
        ).fetchone()

        memories = connection.execute(
            """
            SELECT message_text, created_at
            FROM message_memory
            WHERE discord_user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (discord_user_id, message_limit),
        ).fetchall()

        if user is None:
            return _format_context(
                display_name="neregistrovaný používateľ",
                today=today,
                plans=[],
                commitments=[],
                joker=None,
                memories=memories,
            )

        plans = connection.execute(
            """
            SELECT id, workout_type, planned_day, planned_time, status
            FROM weekly_plans
            WHERE user_id = ? AND week_start = ?
            ORDER BY planned_day ASC, planned_time ASC
            """,
            (user["id"], week_start),
        ).fetchall()
        commitments = connection.execute(
            """
            SELECT workout_type, count_per_week
            FROM commitments
            WHERE user_id = ? AND is_active = 1
            ORDER BY workout_type ASC
            """,
            (user["id"],),
        ).fetchall()
        joker = connection.execute(
            """
            SELECT weekly_plan_id, new_day, new_time
            FROM jokers
            WHERE user_id = ? AND week_start = ?
            """,
            (user["id"], week_start),
        ).fetchone()

    enriched_plans = []
    monday = date.fromisoformat(week_start)
    for plan in plans:
        day_number = DAY_ORDER.get(plan["planned_day"])
        planned_date = (
            monday + timedelta(days=day_number - 1) if day_number is not None else None
        )
        relative_label = ""
        if planned_date == today:
            relative_label = " DNES"
        elif planned_date == tomorrow:
            relative_label = " ZAJTRA"

        enriched_plans.append(
            {
                **dict(plan),
                "planned_date": planned_date.isoformat() if planned_date else "neznámy",
                "relative_label": relative_label,
            }
        )
    enriched_plans.sort(
        key=lambda plan: (
            DAY_ORDER.get(plan["planned_day"], 99),
            plan["planned_time"],
        )
    )

    return _format_context(
        display_name=user["display_name"],
        today=today,
        plans=enriched_plans,
        commitments=commitments,
        joker=joker,
        memories=memories,
    )


def _format_context(
    display_name: str,
    today: date,
    plans,
    commitments,
    joker,
    memories,
) -> str:
    lines = [
        "KONTEXT Z DATABÁZY - používaj ho iba na rozpoznanie odkazov:",
        f"Aktuálny používateľ: {display_name}",
        f"Dnes: {today.isoformat()}",
        f"Zajtra: {(today + timedelta(days=1)).isoformat()}",
        "",
        "Plán aktuálneho týždňa:",
    ]

    if plans:
        for plan in plans:
            lines.append(
                f"- plan_id={plan['id']}; typ={plan['workout_type']}; "
                f"deň={plan['planned_day']}; dátum={plan['planned_date']}"
                f"{plan['relative_label']}; čas={plan['planned_time']}; "
                f"status={plan['status']}"
            )
    else:
        lines.append("- bez tréningov")

    lines.extend(["", "Aktívne záväzky:"])
    if commitments:
        for commitment in commitments:
            lines.append(
                f"- {commitment['workout_type']}: "
                f"{commitment['count_per_week']}x týždenne"
            )
    else:
        lines.append("- bez záväzkov")

    lines.extend(["", "Žolík tento týždeň:"])
    if joker is None:
        lines.append("- nepoužitý")
    else:
        lines.append(
            f"- použitý na plan_id={joker['weekly_plan_id']}, "
            f"nový termín={joker['new_day']} {joker['new_time']}"
        )

    lines.extend(["", "Predchádzajúce správy používateľa, najnovšia prvá:"])
    if memories:
        for memory in memories:
            lines.append(f"- {memory['message_text']}")
    else:
        lines.append("- bez uloženej histórie")

    return "\n".join(lines)
