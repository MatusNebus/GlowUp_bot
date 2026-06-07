from datetime import datetime, timezone

from app.database import get_connection


def register_user(discord_user_id: str, display_name: str) -> tuple[bool, str]:
    """Zaregistruje Discord používateľa, ak ešte nie je v databáze."""
    clean_name = display_name.strip()
    if not clean_name:
        return False, "Chýba meno. Skús napríklad: jonas register Matúš"

    with get_connection() as connection:
        existing_user = connection.execute(
            """
            SELECT display_name
            FROM users
            WHERE discord_user_id = ?
            """,
            (discord_user_id,),
        ).fetchone()

        if existing_user:
            return False, f"{existing_user['display_name']} už je registrovaný."

        connection.execute(
            """
            INSERT INTO users (discord_user_id, display_name, created_at)
            VALUES (?, ?, ?)
            """,
            (
                discord_user_id,
                clean_name,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    return True, f"{clean_name} je registrovaný. Začni cez: jonas onboarding start"


def list_users() -> list[dict]:
    """Vráti všetkých aktívnych registrovaných používateľov."""
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, discord_user_id, display_name, created_at, is_active
            FROM users
            WHERE is_active = 1
            ORDER BY created_at ASC
            """
        ).fetchall()

    return [dict(row) for row in rows]
