import re
import unicodedata
from datetime import date, datetime, timezone

from app.database import get_connection
from app.services.commitments_service import WALK_REJECTION_MESSAGE


FORBIDDEN_WALK_TYPES = {
    "prechadzka",
    "prechádzka",
    "walk",
    "walking",
    "chôdza",
    "chodza",
}
VALID_DAYS = {
    "pondelok",
    "utorok",
    "streda",
    "stvrtok",
    "piatok",
    "sobota",
    "nedela",
}
DAY_ORDER = {
    "pondelok": 1,
    "utorok": 2,
    "streda": 3,
    "stvrtok": 4,
    "piatok": 5,
    "sobota": 6,
    "nedela": 7,
}
PLAN_FORMAT_MESSAGE = (
    "Správny formát je: jonas plan <typ> <deň> <čas>, "
    "napríklad: jonas plan beh piatok 18:00"
)


def get_current_week_start() -> str:
    """Vráti pondelok aktuálneho týždňa vo formáte YYYY-MM-DD."""
    today = date.today()
    monday = date.fromordinal(today.toordinal() - today.weekday())
    return monday.isoformat()


def normalize_day(day_text: str) -> str:
    """Normalizuje slovenský deň na lowercase bez diakritiky."""
    normalized = _strip_accents(day_text.strip().casefold())
    if normalized not in VALID_DAYS:
        raise ValueError("Neznámy deň v týždni.")
    return normalized


def is_forbidden_walk_type(workout_type: str) -> bool:
    normalized_type = workout_type.strip().casefold()
    return normalized_type in FORBIDDEN_WALK_TYPES


def add_plan(
    discord_user_id: str, workout_type: str, planned_day: str, planned_time: str
) -> tuple[bool, str]:
    """Pridá tréning do aktuálneho týždenného plánu podľa existujúceho záväzku."""
    normalized_type = workout_type.strip().casefold()

    if is_forbidden_walk_type(normalized_type):
        return False, WALK_REJECTION_MESSAGE

    if not normalized_type:
        return False, PLAN_FORMAT_MESSAGE

    try:
        normalized_day = normalize_day(planned_day)
    except ValueError:
        return False, "Deň musí byť jeden zo slovenských dní. " + PLAN_FORMAT_MESSAGE

    if not is_valid_time(planned_time):
        return False, "Čas musí byť vo formáte HH:MM, napríklad 18:00."

    week_start = get_current_week_start()

    with get_connection() as connection:
        user = _get_user(connection, discord_user_id)
        if user is None:
            return False, "Najprv sa musíš registrovať. Skús: jonas register Matúš"

        commitment = connection.execute(
            """
            SELECT count_per_week
            FROM commitments
            WHERE user_id = ? AND workout_type = ? AND is_active = 1
            """,
            (user["id"], normalized_type),
        ).fetchone()

        if commitment is None:
            return (
                False,
                "Tento typ tréningu ešte nemáš v týždennom záväzku. "
                "Najprv si ho nastav cez: jonas commitment <typ> <počet>",
            )

        planned_count = connection.execute(
            """
            SELECT COUNT(*) AS plan_count
            FROM weekly_plans
            WHERE user_id = ?
              AND week_start = ?
              AND workout_type = ?
              AND status != 'missed'
            """,
            (user["id"], week_start, normalized_type),
        ).fetchone()["plan_count"]

        if planned_count >= commitment["count_per_week"]:
            return (
                False,
                "Tento typ tréningu už máš na tento týždeň naplánovaný podľa záväzku. "
                "Ak chceš bonusový tréning, doplníme to neskôr.",
            )

        cursor = connection.execute(
            """
            INSERT INTO weekly_plans (
                user_id,
                week_start,
                workout_type,
                planned_day,
                planned_time,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                week_start,
                normalized_type,
                normalized_day,
                planned_time,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        plan_id = cursor.lastrowid
        plan_ref = _get_plan_reference(connection, user["id"], plan_id)

    return (
        True,
        f"{user['display_name']} má naplánované [{plan_ref}]: "
        f"{normalized_type}, {normalized_day} o {planned_time}.",
    )


def list_my_week(discord_user_id: str) -> tuple[bool, str]:
    """Vypíše plán autora správy na aktuálny týždeň."""
    with get_connection() as connection:
        user = _get_user(connection, discord_user_id)
        if user is None:
            return False, "Najprv sa musíš registrovať. Skús: jonas register Matúš"

        plans = _fetch_week_plans(connection, user["id"])

    if not plans:
        return True, f"{user['display_name']} zatiaľ nemá plán na aktuálny týždeň."

    return True, _format_week_plans(plans, f"Plán pre {user['display_name']}:", False)


def list_all_week() -> str:
    """Vypíše plán všetkých používateľov na aktuálny týždeň."""
    with get_connection() as connection:
        plans = _fetch_week_plans(connection)

    if not plans:
        return "Zatiaľ nie je naplánovaný žiadny tréning na aktuálny týždeň."

    return _format_week_plans(plans, "Týždenný plán:", True)


def weekly_status(discord_user_id: str) -> tuple[bool, str]:
    """Porovná záväzky používateľa s naplánovanými tréningmi v aktuálnom týždni."""
    week_start = get_current_week_start()

    with get_connection() as connection:
        user = _get_user(connection, discord_user_id)
        if user is None:
            return False, "Najprv sa musíš registrovať. Skús: jonas register Matúš"

        commitments = connection.execute(
            """
            SELECT workout_type, count_per_week
            FROM commitments
            WHERE user_id = ? AND is_active = 1
            ORDER BY workout_type ASC
            """,
            (user["id"],),
        ).fetchall()

        if not commitments:
            return (
                True,
                "Zatiaľ nemáš nastavené žiadne týždenné záväzky. "
                "Skús: jonas commitment beh 2",
            )

        planned_counts = connection.execute(
            """
            SELECT workout_type, COUNT(*) AS plan_count
            FROM weekly_plans
            WHERE user_id = ?
              AND week_start = ?
              AND status != 'missed'
            GROUP BY workout_type
            """,
            (user["id"], week_start),
        ).fetchall()

    counts_by_type = {row["workout_type"]: row["plan_count"] for row in planned_counts}

    lines = [f"{user['display_name']} — stav plánovania:"]
    for commitment in commitments:
        workout_type = commitment["workout_type"]
        required_count = commitment["count_per_week"]
        planned_count = counts_by_type.get(workout_type, 0)
        missing_count = max(required_count - planned_count, 0)
        suffix = "hotovo" if missing_count == 0 else f"chýba {missing_count}"

        lines.append(
            f"{workout_type}: {planned_count}/{required_count} naplánované, {suffix}"
        )

    return True, "\n".join(lines)


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def is_valid_time(time_text: str) -> bool:
    if re.fullmatch(r"\d{2}:\d{2}", time_text) is None:
        return False

    hours, minutes = time_text.split(":")
    return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59


def _is_valid_time(time_text: str) -> bool:
    return is_valid_time(time_text)


def _get_user(connection, discord_user_id: str):
    return connection.execute(
        """
        SELECT id, display_name
        FROM users
        WHERE discord_user_id = ? AND is_active = 1
        """,
        (discord_user_id,),
    ).fetchone()


def _fetch_week_plans(connection, user_id: int | None = None):
    week_start = get_current_week_start()
    user_filter = ""
    parameters: list[object] = [week_start]

    if user_id is not None:
        user_filter = "AND users.id = ?"
        parameters.append(user_id)

    rows = connection.execute(
        f"""
        SELECT
            weekly_plans.id,
            users.id AS user_id,
            users.discord_user_id,
            users.display_name,
            weekly_plans.week_start,
            weekly_plans.workout_type,
            weekly_plans.planned_day,
            weekly_plans.planned_time,
            weekly_plans.status
        FROM weekly_plans
        JOIN users ON users.id = weekly_plans.user_id
        WHERE weekly_plans.week_start = ?
          AND users.is_active = 1
          {user_filter}
        """,
        parameters,
    ).fetchall()

    return sorted(
        rows,
        key=lambda row: (
            row["display_name"],
            DAY_ORDER.get(row["planned_day"], 99),
            row["planned_time"],
            row["id"],
        ),
    )


def _format_week_plans(plans, title: str, include_name: bool) -> str:
    lines = [title]
    counters: dict[int, int] = {}
    for plan in plans:
        counters[plan["user_id"]] = counters.get(plan["user_id"], 0) + 1
        plan_ref = counters[plan["user_id"]]
        owner = f"{plan['display_name']}: " if include_name else ""
        lines.append(
            f"{owner}[{plan_ref}] "
            f"{plan['planned_day']} {plan['planned_time']} — "
            f"{plan['workout_type']} — {plan['status']}"
        )
    return "\n".join(lines)


def resolve_plan_reference(
    discord_user_id: str, user_facing_ref: int
) -> tuple[bool, int | str]:
    """Preloží týždenné číslo 1..n na interné ID; potom skúsi staré DB ID."""
    with get_connection() as connection:
        user = _get_user(connection, discord_user_id)
        if user is None:
            return False, "Najprv sa musíš registrovať."

        plans = _fetch_week_plans(connection, user["id"])
        if 1 <= user_facing_ref <= len(plans):
            return True, int(plans[user_facing_ref - 1]["id"])

        owned_plan = connection.execute(
            "SELECT id FROM weekly_plans WHERE id = ? AND user_id = ?",
            (user_facing_ref, user["id"]),
        ).fetchone()
        if owned_plan is not None:
            return True, int(owned_plan["id"])

    return False, "Taký tréning som nenašiel. Pozri si čísla cez: jonas my week"


def get_plan_reference(discord_user_id: str, plan_id: int) -> int:
    """Vráti používateľské číslo interného tréningu v aktuálnom týždni."""
    with get_connection() as connection:
        user = _get_user(connection, discord_user_id)
        if user is None:
            return plan_id
        return _get_plan_reference(connection, user["id"], plan_id)


def _get_plan_reference(connection, user_id: int, plan_id: int) -> int:
    plans = _fetch_week_plans(connection, user_id)
    for index, plan in enumerate(plans, start=1):
        if plan["id"] == plan_id:
            return index
    return plan_id
