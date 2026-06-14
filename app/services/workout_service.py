import re
from datetime import datetime, timezone

from app.database import get_connection
from app.services.activity_service import get_activity_fields
from app.services.planning_service import get_plan_reference


EDITABLE_STATUSES = {"planned", "postponed", "unanswered"}


def complete_workout(discord_user_id: str, plan_id: int, result) -> tuple[bool, str]:
    return _log_workout(discord_user_id, plan_id, "completed", result)


def shorten_workout(discord_user_id: str, plan_id: int, result) -> tuple[bool, str]:
    return _log_workout(discord_user_id, plan_id, "shortened", result)


def miss_workout(discord_user_id: str, plan_id: int) -> tuple[bool, str]:
    with get_connection() as connection:
        plan = _get_owned_plan(connection, discord_user_id, plan_id)
        if isinstance(plan, str):
            return False, plan
        if plan["status"] not in EDITABLE_STATUSES:
            return False, "Tento tréning už nemožno upraviť."
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "UPDATE weekly_plans SET status = 'missed', completed_at = ? WHERE id = ?",
            (now, plan_id),
        )
        connection.execute(
            """
            INSERT INTO workout_logs (
                weekly_plan_id, user_id, activity_version_id, status, created_at
            ) VALUES (?, ?, ?, 'missed', ?)
            """,
            (plan_id, plan["user_id"], plan["activity_version_id"], now),
        )
    return True, "Tréning je zapísaný ako vynechaný."


def get_workout_detail(discord_user_id: str, plan_id: int) -> tuple[bool, str]:
    with get_connection() as connection:
        plan = _get_owned_plan(connection, discord_user_id, plan_id)
        if isinstance(plan, str):
            return False, plan
        log = connection.execute(
            "SELECT * FROM workout_logs WHERE weekly_plan_id = ?", (plan_id,)
        ).fetchone()
        values = []
        if log:
            values = connection.execute(
                """
                SELECT f.display_name, f.unit, f.field_type, v.value_text, v.value_number
                FROM workout_log_values v
                JOIN activity_fields f ON f.id = v.activity_field_id
                WHERE v.workout_log_id = ?
                ORDER BY f.position
                """,
                (log["id"],),
            ).fetchall()
    ref = get_plan_reference(discord_user_id, plan_id)
    lines = [
        f"Tréning [{ref}]",
        f"{plan['planned_day']} {plan['planned_time']} - {plan['workout_type']} - {plan['status']}",
    ]
    if log is None:
        lines.append("Výsledok ešte nie je zapísaný.")
    else:
        lines.append(f"Zápis: {log['status']}")
        for value in values:
            shown = (
                value["value_text"]
                if value["field_type"] == "text"
                else _format_number(value["value_number"])
            )
            unit = f" {value['unit']}" if value["unit"] else ""
            lines.append(f"{value['display_name']}: {shown}{unit}")
    return True, "\n".join(lines)


def _log_workout(discord_user_id: str, plan_id: int, status: str, result) -> tuple[bool, str]:
    with get_connection() as connection:
        plan = _get_owned_plan(connection, discord_user_id, plan_id)
        if isinstance(plan, str):
            return False, plan
        if plan["status"] not in EDITABLE_STATUSES:
            return False, "Tento tréning už nemožno upraviť."
        fields = get_activity_fields(plan["activity_version_id"], connection)
        parsed, errors = _parse_values(result, fields)
        if errors:
            return False, "Potrebujem všetky výsledky: " + "; ".join(errors)
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "UPDATE weekly_plans SET status = ?, completed_at = ? WHERE id = ?",
            (status, now, plan_id),
        )
        cursor = connection.execute(
            """
            INSERT INTO workout_logs (
                weekly_plan_id, user_id, activity_version_id, status, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (plan_id, plan["user_id"], plan["activity_version_id"], status, now),
        )
        for field, value_text, value_number in parsed:
            connection.execute(
                """
                INSERT INTO workout_log_values (
                    workout_log_id, activity_field_id, value_text, value_number
                ) VALUES (?, ?, ?, ?)
                """,
                (cursor.lastrowid, field["id"], value_text, value_number),
            )
    message = "Skrátený tréning aj výsledky sú zapísané." if status == "shortened" else "Tréning aj všetky výsledky sú zapísané."
    return True, message


def _parse_values(result, fields):
    provided = {}
    if isinstance(result, list):
        provided = {
            str(item.get("field_key", "")).casefold(): item.get("value")
            for item in result
            if isinstance(item, dict)
        }
    elif isinstance(result, dict):
        provided = {str(key).casefold(): value for key, value in result.items()}
    else:
        text = str(result or "").strip()
        if len(fields) == 1:
            provided[fields[0]["field_key"]] = text
        else:
            labelled = re.findall(r"([^,;:]+)\s*:\s*([^,;]+)", text)
            provided = {key.strip().casefold(): value.strip() for key, value in labelled}
            if not provided:
                parts = [part.strip() for part in re.split(r"[;,]", text) if part.strip()]
                if len(parts) == len(fields):
                    provided = {field["field_key"]: part for field, part in zip(fields, parts)}

    parsed, errors = [], []
    for field in fields:
        raw = provided.get(field["field_key"])
        if raw is None:
            raw = provided.get(field["display_name"].casefold())
        if raw is None or str(raw).strip() == "":
            errors.append(field["display_name"])
            continue
        value_text, value_number, error = _coerce_value(raw, field["field_type"])
        if error:
            errors.append(f"{field['display_name']} ({error})")
        else:
            parsed.append((field, value_text, value_number))
    return parsed, errors


def _coerce_value(raw, field_type):
    text = str(raw).strip()
    if field_type == "text":
        return text, None, None
    numbers = re.findall(r"-?\d+(?:[.,]\d+)?", text)
    if not numbers:
        return None, None, "musí byť číslo"
    number = float(numbers[0].replace(",", "."))
    if field_type == "rating" and not 1 <= number <= 10:
        return None, None, "hodnotenie musí byť 1 až 10"
    if field_type in {"number", "duration"} and number < 0:
        return None, None, "hodnota nemôže byť záporná"
    return None, number, None


def _get_owned_plan(connection, discord_user_id: str, plan_id: int):
    plan = connection.execute(
        """
        SELECT p.*, u.discord_user_id
        FROM weekly_plans p JOIN users u ON u.id = p.user_id
        WHERE p.id = ?
        """,
        (plan_id,),
    ).fetchone()
    if plan is None:
        return "Takýto tréning neexistuje."
    if plan["discord_user_id"] != discord_user_id:
        return "Tento tréning nepatrí tebe."
    return plan


def _format_number(value) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0")
