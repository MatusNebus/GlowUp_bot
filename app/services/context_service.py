from datetime import date, datetime, timedelta, timezone

from app.database import get_connection
from app.services.planning_service import DAY_ORDER, get_current_week_start


def save_channel_message(
    discord_user_id: str,
    author_display_name: str,
    channel_id: str,
    message_text: str,
) -> None:
    """Uloží nebot správu z kanála, aby AI rozumela nadväzujúcej konverzácii."""
    clean_text = message_text.strip()
    if not clean_text:
        return

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO message_memory (
                discord_user_id, author_display_name, channel_id, message_text, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                discord_user_id,
                author_display_name,
                channel_id,
                clean_text,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def save_user_message(discord_user_id: str, message_text: str) -> None:
    """Spätná kompatibilita pre staršie volania mimo Discord kanála."""
    save_channel_message(discord_user_id, discord_user_id, "", message_text)


def build_ai_context(
    discord_user_id: str, channel_id: str | None = None, message_limit: int = 10
) -> str:
    """Pripraví stručný kontext používateľa, plánu a posledných správ kanála."""
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

        if channel_id:
            memories = connection.execute(
                """
                SELECT discord_user_id, author_display_name, message_text, created_at
                FROM message_memory
                WHERE channel_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (channel_id, message_limit),
            ).fetchall()
        else:
            memories = connection.execute(
                """
                SELECT discord_user_id, author_display_name, message_text, created_at
                FROM message_memory
                ORDER BY id DESC
                LIMIT ?
                """,
                (message_limit,),
            ).fetchall()

        if user is None:
            return _format_context(
                discord_user_id,
                "neregistrovaný používateľ",
                today,
                [],
                [],
                None,
                memories,
            )

        plans = connection.execute(
            """
            SELECT id, workout_type, planned_day, planned_time, status
            FROM weekly_plans
            WHERE user_id = ? AND week_start = ?
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
    sorted_plans = sorted(
        plans,
        key=lambda plan: (
            DAY_ORDER.get(plan["planned_day"], 99),
            plan["planned_time"],
            plan["id"],
        ),
    )
    for plan_ref, plan in enumerate(sorted_plans, start=1):
        day_number = DAY_ORDER.get(plan["planned_day"])
        planned_date = monday + timedelta(days=day_number - 1) if day_number else None
        relative_label = ""
        if planned_date == today:
            relative_label = " DNES"
        elif planned_date == tomorrow:
            relative_label = " ZAJTRA"
        enriched_plans.append(
            {
                **dict(plan),
                "plan_ref": plan_ref,
                "planned_date": planned_date.isoformat() if planned_date else "neznámy",
                "relative_label": relative_label,
            }
        )

    return _format_context(
        discord_user_id,
        user["display_name"],
        today,
        enriched_plans,
        commitments,
        joker,
        memories,
    )


def _format_context(
    discord_user_id: str,
    display_name: str,
    today: date,
    plans,
    commitments,
    joker,
    memories,
) -> str:
    lines = [
        "KONTEXT PRE AI - používaj ho iba na pochopenie správy:",
        f"Aktuálny používateľ: {display_name} (discord_user_id={discord_user_id})",
        f"Dnes: {today.isoformat()}",
        f"Zajtra: {(today + timedelta(days=1)).isoformat()}",
        "",
        "Plán aktuálneho týždňa:",
    ]
    if plans:
        for plan in plans:
            lines.append(
                f"- používateľské číslo={plan['plan_ref']}; interné_id={plan['id']}; "
                f"typ={plan['workout_type']}; deň={plan['planned_day']}; "
                f"dátum={plan['planned_date']}{plan['relative_label']}; "
                f"čas={plan['planned_time']}; status={plan['status']}"
            )
    else:
        lines.append("- bez tréningov")

    lines.extend(["", "Aktívne záväzky:"])
    if commitments:
        for commitment in commitments:
            lines.append(
                f"- {commitment['workout_type']}: {commitment['count_per_week']}x týždenne"
            )
    else:
        lines.append("- bez záväzkov")

    lines.extend(["", "Žolík tento týždeň:"])
    if joker is None:
        lines.append("- nepoužitý")
    else:
        lines.append(
            f"- použitý na interné_id={joker['weekly_plan_id']}, "
            f"nový termín={joker['new_day']} {joker['new_time']}"
        )

    lines.extend(["", "Posledné správy v kanáli, najnovšia prvá:"])
    if memories:
        for memory in memories:
            author = memory["author_display_name"] or memory["discord_user_id"]
            own = " (aktuálny používateľ)" if memory["discord_user_id"] == discord_user_id else ""
            lines.append(f"- {author}{own}: {memory['message_text']}")
    else:
        lines.append("- bez uloženej histórie")
    return "\n".join(lines)
