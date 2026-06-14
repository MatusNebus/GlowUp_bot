import re
from datetime import date

from app.database import get_connection
from app.services.planning_service import DAY_ORDER


MONTH_FORMAT_MESSAGE = "Mesiac zadaj vo formáte YYYY-MM, napríklad: jonas stats 2026-06"


def get_current_month() -> str:
    return date.today().strftime("%Y-%m")


def get_month_range(month: str) -> tuple[str, str]:
    if re.fullmatch(r"\d{4}-\d{2}", month) is None:
        raise ValueError(MONTH_FORMAT_MESSAGE)
    start = date.fromisoformat(f"{month}-01")
    end = date(start.year + 1, 1, 1) if start.month == 12 else date(start.year, start.month + 1, 1)
    return start.isoformat(), end.isoformat()


def get_user_month_stats(discord_user_id: str, month: str | None = None) -> tuple[bool, str]:
    selected = month or get_current_month()
    try:
        start, end = get_month_range(selected)
    except ValueError:
        return False, MONTH_FORMAT_MESSAGE
    with get_connection() as connection:
        user = connection.execute(
            "SELECT id, display_name FROM users WHERE discord_user_id = ? AND is_active = 1",
            (discord_user_id,),
        ).fetchone()
        if user is None:
            return False, "Najprv sa musíš registrovať."
        report = _user_report(connection, user["id"], user["display_name"], selected, start, end)
    return True, report


def get_all_month_stats(month: str | None = None) -> str:
    selected = month or get_current_month()
    try:
        start, end = get_month_range(selected)
    except ValueError:
        return MONTH_FORMAT_MESSAGE
    with get_connection() as connection:
        users = connection.execute(
            "SELECT id, display_name FROM users WHERE is_active = 1 ORDER BY display_name"
        ).fetchall()
        reports = [_counts(connection, user["id"], start, end) for user in users]
    lines = [f"Spoločné štatistiky za {selected}"]
    for user, counts in zip(users, reports):
        lines.append(
            f"- {user['display_name']}: {counts['success']}/{counts['planned']} úspešných, "
            f"{counts['missed']} vynechaných"
        )
    return "\n".join(lines)


def _user_report(connection, user_id, display_name, month, start, end):
    counts = _counts(connection, user_id, start, end)
    lines = [
        f"Štatistiky za {month} - {display_name}",
        f"Plánované: {counts['planned']}",
        f"Splnené: {counts['completed']}",
        f"Skrátené: {counts['shortened']}",
        f"Vynechané: {counts['missed']}",
        f"Úspešnosť: {counts['rate']:.1f} %",
    ]
    values = connection.execute(
        """
        SELECT p.week_start, p.planned_day, p.workout_type, f.display_name,
               f.unit, f.field_type, v.value_number
        FROM workout_log_values v
        JOIN workout_logs l ON l.id = v.workout_log_id
        JOIN weekly_plans p ON p.id = l.weekly_plan_id
        JOIN activity_fields f ON f.id = v.activity_field_id
        WHERE p.user_id = ? AND v.value_number IS NOT NULL
        """,
        (user_id,),
    ).fetchall()
    totals = {}
    for row in values:
        planned = _planned_date(row["week_start"], row["planned_day"])
        if planned and start <= planned < end:
            key = (row["workout_type"], row["display_name"], row["unit"] or "")
            totals[key] = totals.get(key, 0) + row["value_number"]
    if totals:
        lines.append("Číselné výsledky:")
        for (activity, field, unit), total in sorted(totals.items()):
            lines.append(f"- {activity} / {field}: {_fmt(total)} {unit}".rstrip())
    return "\n".join(lines)


def _counts(connection, user_id, start, end):
    rows = connection.execute(
        "SELECT week_start, planned_day, status FROM weekly_plans WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    statuses = [
        row["status"]
        for row in rows
        if (planned := _planned_date(row["week_start"], row["planned_day"]))
        and start <= planned < end
        and row["status"] != "replaced"
    ]
    success = statuses.count("completed") + statuses.count("shortened")
    return {
        "planned": len(statuses),
        "completed": statuses.count("completed"),
        "shortened": statuses.count("shortened"),
        "missed": statuses.count("missed"),
        "success": success,
        "rate": success / len(statuses) * 100 if statuses else 0,
    }


def _planned_date(week_start, planned_day):
    try:
        monday = date.fromisoformat(week_start)
    except ValueError:
        return None
    day_number = DAY_ORDER.get(planned_day)
    return date.fromordinal(monday.toordinal() + day_number - 1).isoformat() if day_number else None


def _fmt(value):
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0")
