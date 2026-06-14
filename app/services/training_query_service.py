from datetime import date

from app.database import get_connection
from app.services.planning_service import DAY_ORDER


AGGREGATIONS = {"count", "sum", "average", "min", "max"}


def query_training_data(discord_user_id: str, query: dict) -> tuple[bool, str]:
    """Run a validated, read-only training query without accepting SQL."""
    aggregation = str(query.get("aggregation") or "count").casefold()
    if aggregation not in AGGREGATIONS:
        return False, "Nepodporovaná agregácia."
    scope = str(query.get("scope") or "self").casefold()
    if scope not in {"self", "group"}:
        return False, "Rozsah musí byť `self` alebo `group`."
    date_from = str(query.get("date_from") or "0001-01-01")
    date_to = str(query.get("date_to") or "9999-12-31")
    try:
        date.fromisoformat(date_from)
        date.fromisoformat(date_to)
    except ValueError:
        return False, "Dátumy musia byť vo formáte YYYY-MM-DD."

    with get_connection() as connection:
        requester = connection.execute(
            "SELECT id FROM users WHERE discord_user_id = ? AND is_active = 1",
            (discord_user_id,),
        ).fetchone()
        if requester is None:
            return False, "Dáta môže čítať iba registrovaný používateľ."
        rows = connection.execute(
            """
            SELECT p.id, p.user_id, p.week_start, p.planned_day, p.workout_type,
                   p.status, u.display_name, l.id AS log_id, a.slug,
                   current_version.display_name AS current_activity_name
            FROM weekly_plans p
            JOIN users u ON u.id = p.user_id
            JOIN activity_versions plan_version ON plan_version.id = p.activity_version_id
            JOIN activity_types a ON a.id = plan_version.activity_type_id
            JOIN activity_versions current_version ON current_version.id = a.current_version_id
            LEFT JOIN workout_logs l ON l.weekly_plan_id = p.id
            WHERE u.is_active = 1
            """
        ).fetchall()
        selected = []
        statuses = query.get("statuses") or []
        activity = str(query.get("activity") or "").casefold()
        for row in rows:
            planned_date = _planned_date(row["week_start"], row["planned_day"])
            if planned_date is None or not (date_from <= planned_date <= date_to):
                continue
            if scope == "self" and row["user_id"] != requester["id"]:
                continue
            known_names = {
                row["workout_type"].casefold(),
                row["slug"].casefold(),
                row["current_activity_name"].casefold(),
            }
            if activity and activity not in known_names:
                continue
            if statuses and row["status"] not in statuses:
                continue
            selected.append(row)
        if aggregation == "count":
            return True, f"Počet zodpovedajúcich tréningov: {len(selected)}."

        field_key = str(query.get("field_key") or "").casefold()
        if not field_key:
            return False, "Pri tejto agregácii treba určiť parameter aktivity."
        log_ids = [row["log_id"] for row in selected if row["log_id"] is not None]
        if not log_ids:
            return True, "Pre zadaný výber nie sú uložené žiadne číselné výsledky."
        placeholders = ",".join("?" for _ in log_ids)
        values = connection.execute(
            f"""
            SELECT v.value_number, f.display_name, f.unit
            FROM workout_log_values v
            JOIN activity_fields f ON f.id = v.activity_field_id
            WHERE v.workout_log_id IN ({placeholders})
              AND f.field_key = ?
              AND f.field_type IN ('number', 'duration', 'rating')
              AND v.value_number IS NOT NULL
            """,
            (*log_ids, field_key),
        ).fetchall()
    if not values:
        return True, f"Pre parameter `{field_key}` nie sú číselné výsledky."
    numbers = [row["value_number"] for row in values]
    result = {
        "sum": sum(numbers),
        "average": sum(numbers) / len(numbers),
        "min": min(numbers),
        "max": max(numbers),
    }[aggregation]
    unit = values[0]["unit"] or ""
    return True, f"{aggregation} pre {values[0]['display_name']}: {_format_number(result)} {unit}".strip()


def _planned_date(week_start: str, planned_day: str) -> str | None:
    try:
        monday = date.fromisoformat(week_start)
    except ValueError:
        return None
    day_number = DAY_ORDER.get(planned_day)
    if day_number is None:
        return None
    return date.fromordinal(monday.toordinal() + day_number - 1).isoformat()


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0")
