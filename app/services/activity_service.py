import json
import re
import unicodedata
from datetime import datetime, timezone

from app.config import ADMIN_DISCORD_USER_ID
from app.database import get_connection


FIELD_TYPES = {"number", "duration", "text", "rating"}


def create_activity(
    discord_user_id: str, display_name: str, fields: list[dict]
) -> tuple[bool, str]:
    clean_name = display_name.strip()
    normalized_fields = _normalize_fields(fields)
    if not clean_name or not normalized_fields or len(normalized_fields) != len(fields):
        return False, _creation_help()

    slug = _slugify(clean_name)
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        user = _active_user(connection, discord_user_id)
        if user is None:
            return False, "Novú aktivitu môže pridať iba registrovaný používateľ."
        existing = connection.execute(
            """
            SELECT a.id
            FROM activity_types a
            LEFT JOIN activity_versions v ON v.id = a.current_version_id
            WHERE a.slug = ? OR lower(v.display_name) = lower(?)
            """,
            (slug, clean_name),
        ).fetchone()
        if existing:
            return False, f"Aktivita `{clean_name}` už existuje."
        cursor = connection.execute(
            """
            INSERT INTO activity_types (slug, created_by_discord_user_id, created_at)
            VALUES (?, ?, ?)
            """,
            (slug, discord_user_id, now),
        )
        activity_id = cursor.lastrowid
        version_id = _insert_version(
            connection, activity_id, 1, clean_name, normalized_fields, discord_user_id, now
        )
        connection.execute(
            "UPDATE activity_types SET current_version_id = ? WHERE id = ?",
            (version_id, activity_id),
        )
    return True, f"Aktivita `{clean_name}` bola pridaná. Zapisuje sa: {_field_summary(normalized_fields)}."


def list_activities(active_only: bool = True) -> list[dict]:
    active_filter = "WHERE a.is_active = 1" if active_only else ""
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT a.id, a.slug, a.is_active, a.current_version_id,
                   v.display_name, v.version_number
            FROM activity_types a
            JOIN activity_versions v ON v.id = a.current_version_id
            {active_filter}
            ORDER BY v.display_name COLLATE NOCASE
            """
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["fields"] = get_activity_fields(int(row["current_version_id"]), connection)
            result.append(item)
    return result


def format_activities() -> str:
    activities = list_activities()
    if not activities:
        return (
            "Zatiaľ nie je pridaná žiadna aktivita. "
            "Napíš napríklad: pridaj aktivitu beh, zapisuj kilometre, trvanie a pocit."
        )
    lines = ["Aktívne typy tréningov:"]
    for activity in activities:
        lines.append(f"- {activity['display_name']}: {_field_summary(activity['fields'])}")
    return "\n".join(lines)


def get_active_activity(reference: str, connection=None):
    own_connection = connection is None
    connection = connection or get_connection()
    try:
        slug = _slugify(reference)
        return connection.execute(
            """
            SELECT a.id, a.slug, a.current_version_id, v.display_name
            FROM activity_types a
            JOIN activity_versions v ON v.id = a.current_version_id
            WHERE a.is_active = 1
              AND (a.slug = ? OR lower(v.display_name) = lower(?))
            """,
            (slug, reference.strip()),
        ).fetchone()
    finally:
        if own_connection:
            connection.close()


def get_activity_fields(version_id: int, connection=None) -> list[dict]:
    own_connection = connection is None
    connection = connection or get_connection()
    try:
        rows = connection.execute(
            """
            SELECT id, field_key, display_name, field_type, unit, position
            FROM activity_fields
            WHERE activity_version_id = ?
            ORDER BY position
            """,
            (version_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if own_connection:
            connection.close()


def request_activity_change(
    discord_user_id: str,
    activity_name: str,
    change_type: str,
    proposed_name: str | None = None,
    fields: list[dict] | None = None,
) -> tuple[bool, str]:
    if not ADMIN_DISCORD_USER_ID:
        return False, "ADMIN_DISCORD_USER_ID nie je nastavené, zmenu aktivity nemožno schváliť."
    if change_type not in {"edit", "deactivate"}:
        return False, "Neznámy typ zmeny aktivity."
    proposed_fields = _normalize_fields(fields or []) if change_type == "edit" else []
    if change_type == "edit" and fields and len(proposed_fields) != len(fields):
        return False, "Každý parameter musí mať jedinečný názov a typ number, duration, text alebo rating."
    with get_connection() as connection:
        if _active_user(connection, discord_user_id) is None:
            return False, "Zmenu môže navrhnúť iba registrovaný používateľ."
        activity = get_active_activity(activity_name, connection)
        if activity is None:
            return False, f"Aktívnu aktivitu `{activity_name}` som nenašiel."
        if change_type == "edit" and not (proposed_name or proposed_fields):
            return False, "Pri úprave uveď nový názov alebo kompletný nový zoznam parametrov."
        target_name = (proposed_name or activity["display_name"]).strip()
        duplicate = connection.execute(
            """
            SELECT a.id
            FROM activity_types a
            JOIN activity_versions v ON v.id = a.current_version_id
            WHERE a.id != ? AND lower(v.display_name) = lower(?)
            """,
            (activity["id"], target_name),
        ).fetchone()
        if duplicate:
            return False, f"Aktivita s názvom `{target_name}` už existuje."
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "display_name": target_name,
            "fields": proposed_fields or get_activity_fields(activity["current_version_id"], connection),
        }
        cursor = connection.execute(
            """
            INSERT INTO activity_change_requests (
                requester_discord_user_id, activity_type_id, change_type,
                proposed_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (discord_user_id, activity["id"], change_type, json.dumps(payload, ensure_ascii=False), now),
        )
        request_id = cursor.lastrowid
    action = "deaktiváciu" if change_type == "deactivate" else "úpravu"
    return True, f"<@{ADMIN_DISCORD_USER_ID}> návrh #{request_id} čaká na schválenie: {action} aktivity `{activity['display_name']}`."


def resolve_activity_change(
    discord_user_id: str, request_id: int, approve: bool
) -> tuple[bool, str]:
    if not ADMIN_DISCORD_USER_ID or discord_user_id != ADMIN_DISCORD_USER_ID.strip():
        return False, "Túto zmenu môže schváliť iba admin."
    with get_connection() as connection:
        request = connection.execute(
            "SELECT * FROM activity_change_requests WHERE id = ? AND status = 'open'",
            (request_id,),
        ).fetchone()
        if request is None:
            return False, "Otvorený návrh zmeny aktivity s týmto ID neexistuje."
        now = datetime.now(timezone.utc).isoformat()
        if not approve:
            connection.execute(
                """
                UPDATE activity_change_requests
                SET status = 'rejected', resolved_at = ?, resolved_by_discord_user_id = ?
                WHERE id = ?
                """,
                (now, discord_user_id, request_id),
            )
            return True, f"Návrh zmeny aktivity #{request_id} bol odmietnutý."
        if request["change_type"] == "deactivate":
            connection.execute(
                "UPDATE activity_types SET is_active = 0, deactivated_at = ? WHERE id = ?",
                (now, request["activity_type_id"]),
            )
            connection.execute(
                "UPDATE commitments SET is_active = 0 WHERE activity_type_id = ?",
                (request["activity_type_id"],),
            )
        else:
            payload = json.loads(request["proposed_json"])
            current = connection.execute(
                "SELECT current_version_id FROM activity_types WHERE id = ?",
                (request["activity_type_id"],),
            ).fetchone()
            version_number = connection.execute(
                "SELECT version_number FROM activity_versions WHERE id = ?",
                (current["current_version_id"],),
            ).fetchone()["version_number"] + 1
            version_id = _insert_version(
                connection,
                request["activity_type_id"],
                version_number,
                payload["display_name"],
                _normalize_fields(payload["fields"]),
                discord_user_id,
                now,
            )
            connection.execute(
                "UPDATE activity_types SET current_version_id = ? WHERE id = ?",
                (version_id, request["activity_type_id"]),
            )
            connection.execute(
                "UPDATE commitments SET workout_type = ? WHERE activity_type_id = ?",
                (payload["display_name"], request["activity_type_id"]),
            )
        connection.execute(
            """
            UPDATE activity_change_requests
            SET status = 'approved', resolved_at = ?, resolved_by_discord_user_id = ?
            WHERE id = ?
            """,
            (now, discord_user_id, request_id),
        )
    return True, f"Návrh zmeny aktivity #{request_id} bol schválený a aplikovaný."


def format_result_prompt(version_id: int) -> str:
    fields = get_activity_fields(version_id)
    parts = []
    for field in fields:
        unit = f" ({field['unit']})" if field["unit"] else ""
        parts.append(f"{field['display_name']}{unit}")
    return ", ".join(parts)


def _insert_version(connection, activity_id, version_number, display_name, fields, creator, now):
    cursor = connection.execute(
        """
        INSERT INTO activity_versions (
            activity_type_id, version_number, display_name,
            created_by_discord_user_id, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (activity_id, version_number, display_name, creator, now),
    )
    version_id = cursor.lastrowid
    for position, field in enumerate(fields, start=1):
        connection.execute(
            """
            INSERT INTO activity_fields (
                activity_version_id, field_key, display_name, field_type, unit, position
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                field["field_key"],
                field["display_name"],
                field["field_type"],
                field.get("unit"),
                position,
            ),
        )
    return version_id


def _normalize_fields(fields: list[dict]) -> list[dict]:
    normalized = []
    used = set()
    for field in fields:
        name = str(field.get("display_name") or field.get("name") or "").strip()
        field_type = str(field.get("field_type") or field.get("type") or "").strip().casefold()
        if not name or field_type not in FIELD_TYPES:
            continue
        key = _slugify(str(field.get("field_key") or name))
        if key in used:
            continue
        used.add(key)
        unit = str(field.get("unit") or "").strip() or None
        normalized.append(
            {"field_key": key, "display_name": name, "field_type": field_type, "unit": unit}
        )
    return normalized


def _field_summary(fields: list[dict]) -> str:
    return ", ".join(
        f"{field['display_name']} [{field['field_type']}{f', {field.get('unit')}' if field.get('unit') else ''}]"
        for field in fields
    )


def _active_user(connection, discord_user_id: str):
    return connection.execute(
        "SELECT id FROM users WHERE discord_user_id = ? AND is_active = 1",
        (discord_user_id,),
    ).fetchone()


def _slugify(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", ascii_text).strip("_")


def _creation_help() -> str:
    return (
        "Potrebujem názov aktivity aj parametre. Napíš ich v jednej správe, napríklad: "
        "pridaj aktivitu beh; kilometre ako číslo v km, čas ako trvanie v minútach "
        "a pocit ako hodnotenie."
    )
