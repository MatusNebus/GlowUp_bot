from datetime import datetime, timezone

from app.database import get_connection


WALK_TYPES = {"prechadzka", "prechádzka", "walk", "walking", "chôdza", "chodza"}
WALK_REJECTION_MESSAGE = (
    "Prechádzka sa podľa pravidiel Couple GlowUp neráta ako tréning. "
    "Môže byť bonus alebo regenerácia, ale nemôže nahradiť povinný tréning."
)


def _normalize_workout_type(workout_type: str) -> str:
    return workout_type.strip().casefold()


def set_commitment(
    discord_user_id: str, workout_type: str, count_per_week: int
) -> tuple[bool, str]:
    """Nastaví pevný týždenný tréningový záväzok používateľa."""
    normalized_type = _normalize_workout_type(workout_type)

    if normalized_type in WALK_TYPES:
        return False, WALK_REJECTION_MESSAGE

    if not normalized_type:
        return False, "Chýba typ tréningu. Skús napríklad: jonas commitment beh 2"

    if count_per_week <= 0:
        return False, "Počet tréningov za týždeň musí byť väčší ako 0."

    with get_connection() as connection:
        user = connection.execute(
            """
            SELECT id, display_name
            FROM users
            WHERE discord_user_id = ? AND is_active = 1
            """,
            (discord_user_id,),
        ).fetchone()

        if user is None:
            return False, "Najprv sa musíš registrovať. Skús: jonas register Matúš"

        existing_commitment = connection.execute(
            """
            SELECT id
            FROM commitments
            WHERE user_id = ? AND workout_type = ? AND is_active = 1
            """,
            (user["id"], normalized_type),
        ).fetchone()

        if existing_commitment:
            connection.execute(
                """
                UPDATE commitments
                SET count_per_week = ?
                WHERE id = ?
                """,
                (count_per_week, existing_commitment["id"]),
            )
            return (
                True,
                f"{user['display_name']} má aktualizovaný záväzok: "
                f"{normalized_type} {count_per_week}x týždenne.",
            )

        connection.execute(
            """
            INSERT INTO commitments (user_id, workout_type, count_per_week, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                user["id"],
                normalized_type,
                count_per_week,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    return (
        True,
        f"{user['display_name']} má nový záväzok: "
        f"{normalized_type} {count_per_week}x týždenne.",
    )


def list_commitments(discord_user_id: str | None = None) -> list[dict]:
    """Vráti aktívne záväzky jedného alebo všetkých aktívnych používateľov."""
    parameters = []
    user_filter = ""

    if discord_user_id is not None:
        user_filter = "AND users.discord_user_id = ?"
        parameters.append(discord_user_id)

    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT
                commitments.id,
                commitments.user_id,
                users.discord_user_id,
                users.display_name,
                commitments.workout_type,
                commitments.count_per_week,
                commitments.created_at,
                commitments.is_active
            FROM commitments
            JOIN users ON users.id = commitments.user_id
            WHERE commitments.is_active = 1
              AND users.is_active = 1
              {user_filter}
            ORDER BY users.display_name ASC, commitments.workout_type ASC
            """,
            parameters,
        ).fetchall()

    return [dict(row) for row in rows]
