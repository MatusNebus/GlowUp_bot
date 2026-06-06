import re
from datetime import date, timedelta

from app.database import get_connection
from app.services.planning_service import DAY_ORDER, is_forbidden_walk_type


MONTH_FORMAT_MESSAGE = (
    "Mesiac zadaj vo formáte YYYY-MM, napríklad: jonas stats 2026-06"
)
SUCCESS_STATUSES = {"completed", "shortened"}
STRENGTH_TYPES = {"posilka", "domaci_trening"}


def get_current_month() -> str:
    """Vráti aktuálny mesiac vo formáte YYYY-MM."""
    return date.today().strftime("%Y-%m")


def get_month_range(month: str) -> tuple[str, str]:
    """Vráti prvý deň mesiaca a prvý deň nasledujúceho mesiaca."""
    if re.fullmatch(r"\d{4}-\d{2}", month) is None:
        raise ValueError(MONTH_FORMAT_MESSAGE)

    try:
        start = date.fromisoformat(f"{month}-01")
    except ValueError as error:
        raise ValueError(MONTH_FORMAT_MESSAGE) from error

    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)

    return start.isoformat(), end.isoformat()


def get_user_month_stats(
    discord_user_id: str, month: str | None = None
) -> tuple[bool, str]:
    """Vráti mesačný report jedného aktívneho používateľa."""
    selected_month = month or get_current_month()
    try:
        get_month_range(selected_month)
    except ValueError:
        return False, MONTH_FORMAT_MESSAGE

    with get_connection() as connection:
        user = connection.execute(
            """
            SELECT id, discord_user_id, display_name
            FROM users
            WHERE discord_user_id = ? AND is_active = 1
            """,
            (discord_user_id,),
        ).fetchone()

        if user is None:
            return False, "Najprv sa musíš registrovať. Skús: jonas register Matúš"

        stats = _calculate_user_stats(connection, dict(user), selected_month)

    if stats["planned_count"] == 0:
        return (
            True,
            f"{stats['display_name']} nemá v mesiaci {selected_month} "
            "žiadne plánované tréningy.",
        )

    return True, _format_user_report(stats, selected_month)


def get_all_month_stats(month: str | None = None) -> str:
    """Vráti stručný mesačný report všetkých aktívnych používateľov."""
    selected_month = month or get_current_month()
    try:
        get_month_range(selected_month)
    except ValueError:
        return MONTH_FORMAT_MESSAGE

    with get_connection() as connection:
        users = connection.execute(
            """
            SELECT id, discord_user_id, display_name
            FROM users
            WHERE is_active = 1
            ORDER BY display_name ASC
            """
        ).fetchall()
        stats_by_user = [
            _calculate_user_stats(connection, dict(user), selected_month)
            for user in users
        ]

    total_planned = sum(stats["planned_count"] for stats in stats_by_user)
    if total_planned == 0:
        return f"V mesiaci {selected_month} nemá nikto naplánované žiadne tréningy."

    totals = {
        "planned": total_planned,
        "completed": sum(stats["completed_count"] for stats in stats_by_user),
        "shortened": sum(stats["shortened_count"] for stats in stats_by_user),
        "missed": sum(stats["missed_count"] for stats in stats_by_user),
        "run_km": sum(stats["run_km_total"] for stats in stats_by_user),
        "run_time": sum(stats["run_time_total"] for stats in stats_by_user),
        "sets": sum(stats["set_count_total"] for stats in stats_by_user),
    }
    top_user = max(
        stats_by_user,
        key=lambda stats: (stats["completed_count"], stats["shortened_count"]),
    )

    lines = [
        f"Spoločné štatistiky za {selected_month}",
        "",
        f"Plánované: {totals['planned']}",
        f"Splnené: {totals['completed']}",
        f"Skrátené: {totals['shortened']}",
        f"Vynechané: {totals['missed']}",
        f"Beh spolu: {_format_number(totals['run_km'])} km / "
        f"{_format_number(totals['run_time'])} min",
        f"Série spolu: {totals['sets']}",
        f"Najviac splnených tréningov: {top_user['display_name']} "
        f"({top_user['completed_count']})",
        "",
        "Používatelia:",
    ]

    for stats in stats_by_user:
        lines.append(
            f"- {stats['display_name']}: "
            f"{stats['success_count']}/{stats['planned_count']} úspešných, "
            f"{stats['missed_count']} vynechaných, "
            f"{stats['completion_rate']:.1f} %"
        )

    comparison = _format_matus_ema_comparison(stats_by_user)
    if comparison:
        lines.extend(["", comparison])

    if totals["missed"] == 0:
        lines.extend(
            [
                "",
                "Bez vynechaného tréningu. Oyshi Sushi je odomknuté pre každého, "
                "kto mal aspoň jeden plánovaný tréning.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                f"V skupine je {totals['missed']} vynechaných tréningov. "
                "Sú zapísané, takže sa z nich dá poučiť. Teraz ich neopakovať.",
            ]
        )

    return "\n".join(lines)


def _calculate_user_stats(connection, user: dict, month: str) -> dict:
    plans = _get_user_month_plans(connection, user["id"], month)
    statuses = [plan["status"] for plan in plans]
    successful_plans = [plan for plan in plans if plan["status"] in SUCCESS_STATUSES]
    run_plans = [plan for plan in successful_plans if plan["workout_type"] == "beh"]
    strength_plans = [
        plan for plan in successful_plans if plan["workout_type"] in STRENGTH_TYPES
    ]

    run_km_total = sum((plan["distance_km"] or 0) for plan in run_plans)
    run_time_total = sum((plan["duration_minutes"] or 0) for plan in run_plans)
    planned_count = len(plans)
    completed_count = statuses.count("completed")
    shortened_count = statuses.count("shortened")
    missed_count = statuses.count("missed")
    success_count = completed_count + shortened_count

    return {
        "user_id": user["id"],
        "discord_user_id": user["discord_user_id"],
        "display_name": user["display_name"],
        "planned_count": planned_count,
        "completed_count": completed_count,
        "shortened_count": shortened_count,
        "missed_count": missed_count,
        "unanswered_count": statuses.count("unanswered"),
        "postponed_count": statuses.count("postponed"),
        "joker_count": _get_joker_count(connection, user["id"], month),
        "run_count": len(run_plans),
        "run_km_total": run_km_total,
        "run_time_total": run_time_total,
        "average_pace_min_per_km": (
            run_time_total / run_km_total if run_km_total > 0 else None
        ),
        "strength_count": len(strength_plans),
        "exercise_count_total": sum(
            (plan["exercise_count"] or 0) for plan in strength_plans
        ),
        "set_count_total": sum((plan["set_count"] or 0) for plan in strength_plans),
        "success_count": success_count,
        "completion_rate": (
            success_count / planned_count * 100 if planned_count > 0 else 0.0
        ),
        "perfect_month": missed_count == 0 and planned_count > 0,
    }


def _get_user_month_plans(connection, user_id: int, month: str) -> list[dict]:
    start_text, end_text = get_month_range(month)
    start_date = date.fromisoformat(start_text)
    end_date = date.fromisoformat(end_text)
    rows = connection.execute(
        """
        SELECT
            weekly_plans.id,
            weekly_plans.week_start,
            weekly_plans.planned_day,
            weekly_plans.workout_type,
            weekly_plans.status,
            workout_logs.distance_km,
            workout_logs.duration_minutes,
            workout_logs.exercise_count,
            workout_logs.set_count
        FROM weekly_plans
        LEFT JOIN workout_logs ON workout_logs.weekly_plan_id = weekly_plans.id
        WHERE weekly_plans.user_id = ?
        """,
        (user_id,),
    ).fetchall()

    plans = []
    for row in rows:
        if is_forbidden_walk_type(row["workout_type"]):
            continue

        planned_date = _get_planned_date(row["week_start"], row["planned_day"])
        if planned_date is not None and start_date <= planned_date < end_date:
            plans.append(dict(row))

    return plans


def _get_planned_date(week_start: str, planned_day: str) -> date | None:
    day_number = DAY_ORDER.get(planned_day)
    if day_number is None:
        return None

    try:
        monday = date.fromisoformat(week_start)
    except ValueError:
        return None

    return monday + timedelta(days=day_number - 1)


def _get_joker_count(connection, user_id: int, month: str) -> int:
    start_date, end_date = get_month_range(month)
    return connection.execute(
        """
        SELECT COUNT(*) AS joker_count
        FROM jokers
        WHERE user_id = ?
          AND used_at >= ?
          AND used_at < ?
        """,
        (user_id, start_date, end_date),
    ).fetchone()["joker_count"]


def _format_user_report(stats: dict, month: str) -> str:
    lines = [
        f"Štatistiky za {month} - {stats['display_name']}",
        "",
        f"Plánované tréningy: {stats['planned_count']}",
        f"Splnené: {stats['completed_count']}",
        f"Skrátené: {stats['shortened_count']}",
        f"Vynechané: {stats['missed_count']}",
        f"Nezodpovedané: {stats['unanswered_count']}",
        f"Odložené: {stats['postponed_count']}",
        f"Použité žolíky: {stats['joker_count']}",
        f"Úspešnosť: {stats['completion_rate']:.1f} %",
        "",
        "Beh:",
        f"- počet behov: {stats['run_count']}",
        f"- vzdialenosť: {_format_number(stats['run_km_total'])} km",
        f"- čas: {_format_number(stats['run_time_total'])} min",
        "- priemerné tempo: "
        + (
            f"{stats['average_pace_min_per_km']:.2f} min/km"
            if stats["average_pace_min_per_km"] is not None
            else "bez dát"
        ),
        "",
        "Sila:",
        f"- silové tréningy: {stats['strength_count']}",
        f"- cviky spolu: {stats['exercise_count_total']}",
        f"- série spolu: {stats['set_count_total']}",
        "",
        "Hodnotenie:",
        _get_evaluation(stats),
        "",
        "Oyshi Sushi:",
        _get_reward_message(stats),
    ]

    if stats["planned_count"] >= 8:
        lines.extend(
            [
                "",
                "Poznámka: Tréning nie je poukážka na prejedanie. Daj si normálne "
                "jedlo, vodu a neznehodnoť dnešnú snahu.",
            ]
        )

    return "\n".join(lines)


def _get_evaluation(stats: dict) -> str:
    if stats["missed_count"] == 0:
        return (
            "Žiadny vynechaný tréning. Záväzok držíš a výsledok je zaslúžený."
        )

    if stats["missed_count"] == 1:
        return (
            "Jeden vynechaný tréning je zapísaný. Nie je to koniec sveta, "
            "ale je to presne vec, ktorú nechceme opakovať."
        )

    return (
        f"Vynechané tréningy: {stats['missed_count']}. Toto už nie je náhoda. "
        "Treba upraviť správanie, nie znižovať záväzok."
    )


def _get_reward_message(stats: dict) -> str:
    if stats["perfect_month"]:
        return "Odmena odomknutá: večera v Oyshi Sushi."

    return (
        "Odmena neodomknutá, pretože počet vynechaných tréningov je "
        f"{stats['missed_count']}."
    )


def _format_matus_ema_comparison(stats_by_user: list[dict]) -> str | None:
    matus = None
    ema = None
    for stats in stats_by_user:
        normalized_name = stats["display_name"].strip().casefold()
        if normalized_name in {"matúš", "matus"}:
            matus = stats
        elif normalized_name == "ema":
            ema = stats

    if matus is None or ema is None:
        return None

    return (
        "Matúš vs Ema: "
        f"{matus['success_count']}/{matus['planned_count']} úspešných vs "
        f"{ema['success_count']}/{ema['planned_count']} úspešných; "
        f"vynechané {matus['missed_count']} vs {ema['missed_count']}."
    )


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.1f}"
