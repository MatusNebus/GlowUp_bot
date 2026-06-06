import json
from datetime import datetime, timezone

from app.database import get_connection


def create_pending_action(
    discord_user_id: str,
    intent: str,
    original_message: str,
    missing_fields: list[str],
    parsed: dict,
) -> dict:
    """Uloží novú neúplnú akciu a uzavrie staršie pending akcie používateľa."""
    clear_old_pending_actions(discord_user_id)

    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO pending_actions (
                discord_user_id,
                intent,
                original_message,
                missing_fields,
                parsed_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                discord_user_id,
                intent,
                original_message,
                json.dumps(missing_fields, ensure_ascii=False),
                json.dumps(parsed, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        action_id = cursor.lastrowid

    return get_pending_action(action_id)


def get_latest_pending_action(discord_user_id: str) -> dict | None:
    """Vráti najnovšiu nevyriešenú akciu používateľa."""
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM pending_actions
            WHERE discord_user_id = ? AND is_resolved = 0
            ORDER BY id DESC
            LIMIT 1
            """,
            (discord_user_id,),
        ).fetchone()
    return _deserialize(row)


def get_pending_action(action_id: int) -> dict | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM pending_actions WHERE id = ?",
            (action_id,),
        ).fetchone()
    return _deserialize(row)


def resolve_pending_action(action_id: int) -> None:
    """Označí pending akciu ako úspešne vyriešenú."""
    with get_connection() as connection:
        connection.execute(
            "UPDATE pending_actions SET is_resolved = 1 WHERE id = ?",
            (action_id,),
        )


def clear_old_pending_actions(discord_user_id: str) -> None:
    """Uzavrie staré pending akcie pred vytvorením novej požiadavky."""
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE pending_actions
            SET is_resolved = 1
            WHERE discord_user_id = ? AND is_resolved = 0
            """,
            (discord_user_id,),
        )


def format_pending_action(action: dict | None) -> str:
    if action is None:
        return "Nemáš žiadnu otvorenú pending akciu."

    missing = ", ".join(action["missing_fields"])
    return (
        f"Pending akcia [{action['id']}]\n"
        f"Intent: {action['intent']}\n"
        f"Pôvodná správa: {action['original_message']}\n"
        f"Chýba: {missing or 'nič'}\n"
        f"Parsed JSON: {json.dumps(action['parsed_json'], ensure_ascii=False)}"
    )


def build_pending_context(action: dict | None) -> str:
    """Pripraví otvorenú akciu pre AI parser."""
    if action is None:
        return "OTVORENÁ PENDING AKCIA: žiadna"

    return (
        "OTVORENÁ PENDING AKCIA:\n"
        f"- action_id: {action['id']}\n"
        f"- intent: {action['intent']}\n"
        f"- pôvodná správa: {action['original_message']}\n"
        f"- chýbajúce polia: {', '.join(action['missing_fields'])}\n"
        f"- pôvodný parsed JSON: "
        f"{json.dumps(action['parsed_json'], ensure_ascii=False)}"
    )


def _deserialize(row) -> dict | None:
    if row is None:
        return None

    action = dict(row)
    action["missing_fields"] = json.loads(action["missing_fields"])
    action["parsed_json"] = json.loads(action["parsed_json"])
    return action
