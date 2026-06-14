from datetime import datetime, timezone

from app.database import get_connection


def ensure_user_exists(discord_user_id: str, display_name: str) -> tuple[bool, dict]:
    """Create an active Discord user on first sight and keep the display name fresh."""
    clean_name = display_name.strip() or discord_user_id
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE discord_user_id = ?",
            (discord_user_id,),
        ).fetchone()
        if user is not None:
            connection.execute(
                "UPDATE users SET display_name = ?, is_active = 1 WHERE id = ?",
                (clean_name, user["id"]),
            )
            return False, dict(user)

        cursor = connection.execute(
            """
            INSERT INTO users (
                discord_user_id, display_name, created_at, onboarding_state
            ) VALUES (?, ?, ?, 'needs_commitments')
            """,
            (discord_user_id, clean_name, now),
        )
        user = connection.execute(
            "SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return True, dict(user)


def register_user(discord_user_id: str, display_name: str) -> tuple[bool, str]:
    """Backward-compatible wrapper; normal operation uses automatic registration."""
    created, user = ensure_user_exists(discord_user_id, display_name)
    if not created:
        return False, f"{user['display_name']} už je registrovaný."
    return True, f"{user['display_name']} je automaticky registrovaný."


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
