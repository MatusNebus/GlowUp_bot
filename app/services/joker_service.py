from datetime import datetime, timezone

from app.database import get_connection
from app.services.planning_service import (
    DAY_ORDER,
    get_current_week_start,
    get_plan_reference,
    is_valid_time,
    normalize_day,
)


JOKER_FORMAT_MESSAGE = (
    "Správny formát je: jonas joker <plan_id> <nový_deň> <nový_čas>, "
    "napríklad: jonas joker 3 sobota 10:00"
)
JOKER_USED_MESSAGE = (
    "Žolíka si už tento týždeň použil/a. Ďalší odklad nie je povolený."
)
JOKER_TOO_FAR_MESSAGE = "Žolík môže posunúť tréning maximálne o jeden deň."
SUNDAY_MESSAGE = (
    "Nedeľný tréning sa žolíkom v MVP zatiaľ nedá posunúť do ďalšieho týždňa."
)
LOCKED_STATUS_MESSAGE = (
    "Tento tréning už je completed, shortened alebo missed. Žolíkom sa už posunúť nedá."
)


def use_joker(
    discord_user_id: str, plan_id: int, new_day: str, new_time: str
) -> tuple[bool, str]:
    """Použije týždenného žolíka a posunie jeden plánovaný tréning."""
    try:
        normalized_day = normalize_day(new_day)
    except ValueError:
        return False, "Deň musí byť jeden zo slovenských dní. " + JOKER_FORMAT_MESSAGE

    if not is_valid_time(new_time):
        return False, "Čas musí byť vo formáte HH:MM, napríklad 10:00."

    current_week_start = get_current_week_start()

    with get_connection() as connection:
        user = _get_user(connection, discord_user_id)
        if user is None:
            return False, "Najprv sa musíš registrovať. Skús: jonas register Matúš"

        plan = _get_plan(connection, plan_id)
        if plan is None:
            return False, "Takýto tréning v pláne neexistuje."

        if plan["user_id"] != user["id"]:
            return False, "Tento tréning nepatrí tebe, takže ho nemôžeš posunúť."

        if plan["status"] not in {"planned", "postponed", "unanswered"}:
            return False, LOCKED_STATUS_MESSAGE

        joker = connection.execute(
            """
            SELECT id, new_day, new_time, weekly_plan_id
            FROM jokers
            WHERE user_id = ? AND week_start = ?
            """,
            (user["id"], current_week_start),
        ).fetchone()

        if joker is not None:
            return False, JOKER_USED_MESSAGE

        old_day_order = DAY_ORDER.get(plan["planned_day"])
        new_day_order = DAY_ORDER.get(normalized_day)
        if old_day_order is None or new_day_order is None:
            return False, "Tréning má neznámy deň v pláne. Skús ho naplánovať nanovo."

        if plan["planned_day"] == "nedela" and normalized_day == "pondelok":
            return False, SUNDAY_MESSAGE

        day_shift = new_day_order - old_day_order
        if day_shift < 0 or day_shift > 1:
            return False, JOKER_TOO_FAR_MESSAGE

        used_at = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO jokers (
                user_id,
                week_start,
                weekly_plan_id,
                used_at,
                old_day,
                old_time,
                new_day,
                new_time
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                current_week_start,
                plan["id"],
                used_at,
                plan["planned_day"],
                plan["planned_time"],
                normalized_day,
                new_time,
            ),
        )
        connection.execute(
            """
            UPDATE weekly_plans
            SET planned_day = ?,
                planned_time = ?,
                status = 'postponed',
                joker_used = 1
            WHERE id = ?
            """,
            (normalized_day, new_time, plan["id"]),
        )

    return (
        True,
        "Žolík použitý. Tréning je posunutý z "
        f"{plan['planned_day']} {plan['planned_time']} na {normalized_day} {new_time}. "
        "Toto nebol reset záväzku, iba odklad. V nový termín sa to plní.",
    )


def joker_status(discord_user_id: str) -> tuple[bool, str]:
    """Vráti stav žolíka používateľa v aktuálnom týždni."""
    current_week_start = get_current_week_start()

    with get_connection() as connection:
        user = _get_user(connection, discord_user_id)
        if user is None:
            return False, "Najprv sa musíš registrovať. Skús: jonas register Matúš"

        joker = connection.execute(
            """
            SELECT
                jokers.weekly_plan_id,
                jokers.old_day,
                jokers.old_time,
                jokers.new_day,
                jokers.new_time,
                weekly_plans.workout_type
            FROM jokers
            JOIN weekly_plans ON weekly_plans.id = jokers.weekly_plan_id
            WHERE jokers.user_id = ? AND jokers.week_start = ?
            """,
            (user["id"], current_week_start),
        ).fetchone()

    if joker is None:
        return True, "Žolík tento týždeň ešte máš k dispozícii."

    plan_ref = get_plan_reference(discord_user_id, joker["weekly_plan_id"])
    return (
        True,
        "Žolík už bol tento týždeň použitý na tréning "
        f"[{plan_ref}] {joker['workout_type']}: "
        f"{joker['old_day']} {joker['old_time']} -> "
        f"{joker['new_day']} {joker['new_time']}.",
    )


def _get_user(connection, discord_user_id: str):
    return connection.execute(
        """
        SELECT id, display_name
        FROM users
        WHERE discord_user_id = ? AND is_active = 1
        """,
        (discord_user_id,),
    ).fetchone()


def _get_plan(connection, plan_id: int):
    return connection.execute(
        """
        SELECT
            id,
            user_id,
            week_start,
            workout_type,
            planned_day,
            planned_time,
            status
        FROM weekly_plans
        WHERE id = ?
        """,
        (plan_id,),
    ).fetchone()
