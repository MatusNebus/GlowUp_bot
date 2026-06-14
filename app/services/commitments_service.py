from datetime import datetime, timezone

from app.database import get_connection
from app.services.activity_service import get_active_activity


def set_commitment(
    discord_user_id: str, workout_type: str, count_per_week: int
) -> tuple[bool, str]:
    """Set a weekly commitment only for an active catalog activity."""
    if count_per_week <= 0:
        return False, "Počet tréningov za týždeň musí byť väčší ako 0."

    with get_connection() as connection:
        user = connection.execute(
            "SELECT id, display_name FROM users WHERE discord_user_id = ? AND is_active = 1",
            (discord_user_id,),
        ).fetchone()
        if user is None:
            return False, "Najprv sa musíš registrovať."
        activity = get_active_activity(workout_type, connection)
        if activity is None:
            return False, (
                f"Aktivita `{workout_type}` nie je v aktívnom katalógu. "
                "Najprv ju pridaj aj s parametrami výsledku."
            )
        existing = connection.execute(
            """
            SELECT id FROM commitments
            WHERE user_id = ? AND activity_type_id = ? AND is_active = 1
            """,
            (user["id"], activity["id"]),
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE commitments SET count_per_week = ?, workout_type = ? WHERE id = ?",
                (count_per_week, activity["display_name"], existing["id"]),
            )
            action = "aktualizovaný"
        else:
            connection.execute(
                """
                INSERT INTO commitments (
                    user_id, activity_type_id, workout_type, count_per_week, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    activity["id"],
                    activity["display_name"],
                    count_per_week,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            action = "nový"
    return True, f"{user['display_name']} má {action} záväzok: {activity['display_name']} {count_per_week}x týždenne."


def list_commitments(discord_user_id: str | None = None) -> list[dict]:
    parameters = []
    user_filter = ""
    if discord_user_id is not None:
        user_filter = "AND users.discord_user_id = ?"
        parameters.append(discord_user_id)
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT commitments.*, users.discord_user_id, users.display_name
            FROM commitments
            JOIN users ON users.id = commitments.user_id
            WHERE commitments.is_active = 1 AND users.is_active = 1 {user_filter}
            ORDER BY users.display_name, commitments.workout_type
            """,
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]
