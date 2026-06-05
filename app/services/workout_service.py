import re
from datetime import datetime, timezone

from app.database import get_connection
from app.services.planning_service import is_forbidden_walk_type


RUN_RESULT_FORMAT_MESSAGE = "Pri behu napíš výsledok napríklad: jonas done 3 5.2 32"
EDIT_NOT_READY_MESSAGE = (
    "Tento tréning už je zapísaný. Editáciu výsledkov doplníme neskôr."
)
MISSED_MESSAGE = (
    "Tréning je zapísaný ako vynechaný. Toto nebolo zlyhanie systému, "
    "toto bolo rozhodnutie. Jeden výpadok nie je koniec sveta, "
    "ale opakovanie z toho spraví zvyk."
)


def complete_workout(
    discord_user_id: str, plan_id: int, result_text: str
) -> tuple[bool, str]:
    """Označí plánovaný tréning ako splnený a uloží výsledok."""
    return _log_workout(discord_user_id, plan_id, "completed", result_text)


def shorten_workout(
    discord_user_id: str, plan_id: int, result_text: str
) -> tuple[bool, str]:
    """Označí plánovaný tréning ako skrátený a uloží výsledok."""
    return _log_workout(discord_user_id, plan_id, "shortened", result_text)


def miss_workout(discord_user_id: str, plan_id: int) -> tuple[bool, str]:
    """Označí plánovaný tréning ako vynechaný."""
    with get_connection() as connection:
        plan = _get_owned_plan(connection, discord_user_id, plan_id)
        if isinstance(plan, str):
            return False, plan

        if plan["status"] not in {"planned", "postponed", "unanswered"}:
            return False, EDIT_NOT_READY_MESSAGE

        created_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            UPDATE weekly_plans
            SET status = ?, completed_at = ?
            WHERE id = ?
            """,
            ("missed", created_at, plan_id),
        )
        connection.execute(
            """
            INSERT INTO workout_logs (
                weekly_plan_id,
                user_id,
                workout_type,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (plan["id"], plan["user_id"], plan["workout_type"], "missed", created_at),
        )

    return True, MISSED_MESSAGE


def get_workout_detail(discord_user_id: str, plan_id: int) -> tuple[bool, str]:
    """Vypíše detail tréningu a jeho výsledok, ak existuje."""
    with get_connection() as connection:
        plan = _get_owned_plan(connection, discord_user_id, plan_id)
        if isinstance(plan, str):
            return False, plan

        log = connection.execute(
            """
            SELECT *
            FROM workout_logs
            WHERE weekly_plan_id = ?
            """,
            (plan_id,),
        ).fetchone()

    lines = [
        f"Tréning [{plan['id']}]",
        f"{plan['planned_day']} {plan['planned_time']} — {plan['workout_type']} — {plan['status']}",
    ]

    if log is None:
        lines.append("Výsledok ešte nie je zapísaný.")
        return True, "\n".join(lines)

    lines.append(f"Zápis: {log['status']}")
    if log["distance_km"] is not None and log["duration_minutes"] is not None:
        lines.append(f"Beh: {log['distance_km']} km za {log['duration_minutes']} min")
    if log["exercises_text"]:
        lines.append(f"Cviky: {log['exercises_text']}")
    if log["exercise_count"] is not None:
        lines.append(f"Počet cvikov: {log['exercise_count']}")
    if log["set_count"] is not None:
        lines.append(f"Počet sérií: {log['set_count']}")
    if log["note"]:
        lines.append(f"Poznámka: {log['note']}")

    return True, "\n".join(lines)


def _log_workout(
    discord_user_id: str, plan_id: int, status: str, result_text: str
) -> tuple[bool, str]:
    clean_result = result_text.strip()
    if not clean_result:
        return False, _result_format_message(status)

    with get_connection() as connection:
        plan = _get_owned_plan(connection, discord_user_id, plan_id)
        if isinstance(plan, str):
            return False, plan

        if plan["status"] not in {"planned", "postponed", "unanswered"}:
            return False, EDIT_NOT_READY_MESSAGE

        if is_forbidden_walk_type(plan["workout_type"]):
            return False, "Prechádzka sa nikdy neráta ako tréning."

        log_values = _build_log_values(plan["workout_type"], status, clean_result)
        if isinstance(log_values, str):
            return False, log_values

        created_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            UPDATE weekly_plans
            SET status = ?, completed_at = ?
            WHERE id = ?
            """,
            (status, created_at, plan_id),
        )
        connection.execute(
            """
            INSERT INTO workout_logs (
                weekly_plan_id,
                user_id,
                workout_type,
                status,
                distance_km,
                duration_minutes,
                exercises_text,
                exercise_count,
                set_count,
                note,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan["id"],
                plan["user_id"],
                plan["workout_type"],
                status,
                log_values["distance_km"],
                log_values["duration_minutes"],
                log_values["exercises_text"],
                log_values["exercise_count"],
                log_values["set_count"],
                log_values["note"],
                created_at,
            ),
        )

    if status == "shortened":
        return (
            True,
            "Skrátená verzia je zapísaná. Nebol to plný plán, ale stále si prišiel a odmakal časť.",
        )

    return True, "Tréning je zapísaný ako splnený. Dobrá práca, žiadne výhovorky."


def _build_log_values(workout_type: str, status: str, result_text: str):
    values = {
        "distance_km": None,
        "duration_minutes": None,
        "exercises_text": None,
        "exercise_count": None,
        "set_count": None,
        "note": None,
    }

    if workout_type == "beh":
        run_result = _parse_run_result(result_text)
        if run_result is None:
            return RUN_RESULT_FORMAT_MESSAGE
        values["distance_km"], values["duration_minutes"] = run_result
        return values

    if workout_type in {"posilka", "domaci_trening"}:
        values["exercises_text"] = result_text
        values["exercise_count"] = _count_exercises(result_text)
        values["set_count"] = _count_sets(result_text)
        return values

    values["note"] = result_text
    return values


def _parse_run_result(result_text: str) -> tuple[float, float] | None:
    normalized_text = result_text.replace(",", ".")
    numbers = re.findall(r"\d+(?:\.\d+)?", normalized_text)
    if len(numbers) < 2:
        return None

    distance_km = float(numbers[0])
    duration_minutes = float(numbers[1])
    if distance_km <= 0 or duration_minutes <= 0:
        return None

    return distance_km, duration_minutes


def _count_exercises(result_text: str) -> int | None:
    exercises = [part.strip() for part in result_text.split(";") if part.strip()]
    if not exercises:
        return None
    return len(exercises)


def _count_sets(result_text: str) -> int | None:
    set_counts = re.findall(r"(\d+)\s*[x×]\s*\d+", result_text.casefold())
    if not set_counts:
        return None
    return sum(int(count) for count in set_counts)


def _get_owned_plan(connection, discord_user_id: str, plan_id: int):
    plan = connection.execute(
        """
        SELECT
            weekly_plans.id,
            weekly_plans.user_id,
            weekly_plans.week_start,
            weekly_plans.workout_type,
            weekly_plans.planned_day,
            weekly_plans.planned_time,
            weekly_plans.status,
            users.discord_user_id,
            users.display_name
        FROM weekly_plans
        JOIN users ON users.id = weekly_plans.user_id
        WHERE weekly_plans.id = ?
        """,
        (plan_id,),
    ).fetchone()

    if plan is None:
        return "Takýto tréning v pláne neexistuje."

    if plan["discord_user_id"] != discord_user_id:
        return "Tento tréning nepatrí tebe, takže ho nemôžeš upraviť."

    return plan


def _result_format_message(status: str) -> str:
    if status == "shortened":
        return "Pri skrátenom tréningu napíš výsledok napríklad: jonas short 3 3.0 20"
    return "Pri zápise tréningu napíš výsledok napríklad: jonas done 3 5.2 32"
