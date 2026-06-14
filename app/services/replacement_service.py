import unicodedata
from datetime import date, datetime, timedelta, timezone

from app.database import get_connection
from app.services.pending_actions_service import create_pending_action
from app.services.activity_service import get_active_activity
from app.services.planning_service import (
    get_current_week_start,
    is_valid_time,
    normalize_day,
    resolve_plan_reference,
)


EDITABLE_STATUSES = {"planned", "postponed", "unanswered"}


def request_workout_replacement(
    requester_discord_user_id: str,
    original_ref: str | int | None,
    original_description: str | None,
    replacement_workout_type: str,
    replacement_day: str,
    replacement_time: str,
    reason: str,
) -> tuple[bool, str]:
    """Vytvorí auditovaný návrh náhrady bez okamžitej zmeny plánu."""
    normalized_type = replacement_workout_type.strip().casefold()
    with get_connection() as connection:
        replacement_activity = get_active_activity(normalized_type, connection)
    if replacement_activity is None:
        return False, f"Aktivita `{normalized_type}` nie je v aktívnom katalógu."

    try:
        normalized_day = normalize_day(replacement_day)
    except ValueError:
        return False, "Deň náhradného tréningu nie je platný."
    if not is_valid_time(replacement_time):
        return False, "Čas náhradného tréningu musí byť vo formáte HH:MM."
    if not reason.strip():
        return False, "Pri objektívnej náhrade musíš uviesť dôvod."

    with get_connection() as connection:
        user = connection.execute(
            """
            SELECT id, display_name FROM users
            WHERE discord_user_id = ? AND is_active = 1
            """,
            (requester_discord_user_id,),
        ).fetchone()
        if user is None:
            return False, "Najprv sa musíš registrovať."

    plan_result = _find_original_plan(
        requester_discord_user_id, original_ref, original_description
    )
    if isinstance(plan_result, str):
        return False, plan_result
    plan = plan_result
    if plan["status"] not in EDITABLE_STATUSES:
        return False, "Tento tréning už nemožno nahradiť."

    with get_connection() as connection:
        existing = connection.execute(
            """
            SELECT id FROM workout_replacement_requests
            WHERE original_weekly_plan_id = ? AND status = 'open'
            """,
            (plan["id"],),
        ).fetchone()
        if existing:
            return False, f"Pre tento tréning už existuje otvorená náhrada #{existing['id']}."

        now = datetime.now(timezone.utc).isoformat()
        replacement_week_start = plan["week_start"]
        if plan["planned_day"] == "nedela" and request["replacement_day"] == "pondelok":
            replacement_week_start = (
                date.fromisoformat(plan["week_start"]) + timedelta(days=7)
            ).isoformat()
        cursor = connection.execute(
            """
            INSERT INTO workout_replacement_requests (
                requester_discord_user_id, original_weekly_plan_id,
                replacement_activity_version_id, replacement_workout_type,
                replacement_day, replacement_time,
                reason, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                requester_discord_user_id,
                plan["id"],
                replacement_activity["current_version_id"],
                replacement_activity["display_name"],
                normalized_day,
                replacement_time,
                reason.strip(),
                now,
            ),
        )
        request_id = cursor.lastrowid
        connection.execute(
            """
            INSERT INTO workout_replacement_votes (
                request_id, voter_discord_user_id, vote, created_at
            )
            VALUES (?, ?, 'approve', ?)
            """,
            (request_id, requester_discord_user_id, now),
        )

    applied, message = _apply_if_unanimous(request_id)
    if applied:
        return True, message
    return (
        True,
        f"Návrh náhrady tréningu #{request_id}: {user['display_name']} chce nahradiť "
        f"{plan['workout_type']} v {plan['planned_day']} {plan['planned_time']} za "
        f"{replacement_activity['display_name']} v {normalized_day} {replacement_time}. Dôvod: {reason.strip()}. "
        f"Zmena prejde až po jednomyseľnom súhlase. Hlasujú: {_pending_mentions(request_id)}.\n"
        f"Schválenie: jonas approve replacement {request_id}\n"
        f"Odmietnutie: jonas reject replacement {request_id}",
    )


def approve_replacement(discord_user_id: str, request_id: int) -> tuple[bool, str]:
    """Zapíše súhlas aktívneho používateľa a aplikuje jednomyseľnú náhradu."""
    result = _save_vote(discord_user_id, request_id, "approve")
    if not result[0]:
        return result
    applied, message = _apply_if_unanimous(request_id)
    return (True, message) if applied else result


def reject_replacement(discord_user_id: str, request_id: int) -> tuple[bool, str]:
    """Odmietne návrh bez zmeny pôvodného tréningu."""
    result = _save_vote(discord_user_id, request_id, "reject")
    if not result[0]:
        return result
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE workout_replacement_requests
            SET status = 'rejected', resolved_at = ?
            WHERE id = ? AND status = 'open'
            """,
            (datetime.now(timezone.utc).isoformat(), request_id),
        )
    return True, f"Náhrada #{request_id} bola odmietnutá. Pôvodný tréning zostáva v pláne."


def list_replacements() -> str:
    """Vypíše otvorené návrhy náhrad."""
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT r.id, r.replacement_workout_type, r.replacement_day,
                   r.replacement_time, r.reason, p.workout_type,
                   p.planned_day, p.planned_time, u.display_name
            FROM workout_replacement_requests r
            JOIN weekly_plans p ON p.id = r.original_weekly_plan_id
            JOIN users u ON u.discord_user_id = r.requester_discord_user_id
            WHERE r.status = 'open'
            ORDER BY r.id ASC
            """
        ).fetchall()
    if not rows:
        return "Nie sú otvorené žiadne návrhy náhrad tréningov."
    lines = ["Otvorené návrhy náhrad:"]
    for row in rows:
        lines.append(
            f"#{row['id']} {row['display_name']}: {row['workout_type']} "
            f"{row['planned_day']} {row['planned_time']} -> "
            f"{row['replacement_workout_type']} {row['replacement_day']} "
            f"{row['replacement_time']}; dôvod: {row['reason']}"
        )
    return "\n".join(lines)


def get_replacement_detail(request_id: int) -> str:
    """Vypíše detail requestu a všetky hlasy."""
    with get_connection() as connection:
        request = connection.execute(
            """
            SELECT r.*, p.workout_type AS original_type, p.planned_day AS original_day,
                   p.planned_time AS original_time, u.display_name
            FROM workout_replacement_requests r
            JOIN weekly_plans p ON p.id = r.original_weekly_plan_id
            LEFT JOIN users u ON u.discord_user_id = r.requester_discord_user_id
            WHERE r.id = ?
            """,
            (request_id,),
        ).fetchone()
        if request is None:
            return "Návrh náhrady s týmto ID neexistuje."
        votes = connection.execute(
            """
            SELECT voter_discord_user_id, vote
            FROM workout_replacement_votes
            WHERE request_id = ?
            ORDER BY id ASC
            """,
            (request_id,),
        ).fetchall()

    lines = [
        f"Náhrada #{request['id']} — {request['status']}",
        f"Žiadateľ: {request['display_name'] or request['requester_discord_user_id']}",
        f"Pôvodný tréning: {request['original_type']} {request['original_day']} {request['original_time']}",
        f"Náhrada: {request['replacement_workout_type']} {request['replacement_day']} {request['replacement_time']}",
        f"Dôvod: {request['reason']}",
        "Hlasy:",
    ]
    lines.extend(f"- {vote['voter_discord_user_id']}: {vote['vote']}" for vote in votes)
    return "\n".join(lines)


def _find_original_plan(discord_user_id: str, original_ref, description):
    if original_ref is not None and str(original_ref).strip():
        try:
            success, plan_id = resolve_plan_reference(discord_user_id, int(original_ref))
        except ValueError:
            success, plan_id = False, ""
        if not success:
            return "Pôvodný tréning som nenašiel. Pozri si číslo cez: jonas my week"
        with get_connection() as connection:
            return _owned_plan(connection, discord_user_id, int(plan_id))

    normalized_description = _normalize(description or "")
    if not normalized_description:
        return "Potrebujem číslo alebo popis pôvodného tréningu."
    week_start = get_current_week_start()
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT p.*, u.discord_user_id
            FROM weekly_plans p JOIN users u ON u.id = p.user_id
            WHERE u.discord_user_id = ? AND p.week_start = ?
            """,
            (discord_user_id, week_start),
        ).fetchall()
    matches = []
    for row in rows:
        fields = [row["workout_type"], row["planned_day"], row["planned_time"]]
        score = sum(_normalize(field) in normalized_description for field in fields)
        if score >= 1:
            matches.append(row)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return "Našiel som viac možností. Pozri si číslo cez: jonas my week"
    return "Pôvodný tréning som podľa popisu nenašiel. Pozri si číslo cez: jonas my week"


def _save_vote(discord_user_id: str, request_id: int, vote: str) -> tuple[bool, str]:
    with get_connection() as connection:
        user = connection.execute(
            "SELECT display_name FROM users WHERE discord_user_id = ? AND is_active = 1",
            (discord_user_id,),
        ).fetchone()
        request = connection.execute(
            "SELECT id FROM workout_replacement_requests WHERE id = ? AND status = 'open'",
            (request_id,),
        ).fetchone()
        if user is None:
            return False, "Hlasovať môže iba aktívny registrovaný používateľ."
        if request is None:
            return False, "Otvorená náhrada s týmto ID neexistuje."
        connection.execute(
            """
            INSERT INTO workout_replacement_votes (
                request_id, voter_discord_user_id, vote, created_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(request_id, voter_discord_user_id)
            DO UPDATE SET vote = excluded.vote, created_at = excluded.created_at
            """,
            (request_id, discord_user_id, vote, datetime.now(timezone.utc).isoformat()),
        )
    return True, f"Hlas používateľa {user['display_name']} pre náhradu #{request_id} je zapísaný."


def _apply_if_unanimous(request_id: int) -> tuple[bool, str]:
    with get_connection() as connection:
        request = connection.execute(
            "SELECT * FROM workout_replacement_requests WHERE id = ? AND status = 'open'",
            (request_id,),
        ).fetchone()
        if request is None:
            return False, ""
        active_count = connection.execute(
            "SELECT COUNT(*) AS count FROM users WHERE is_active = 1"
        ).fetchone()["count"]
        approve_count = connection.execute(
            """
            SELECT COUNT(DISTINCT voter_discord_user_id) AS count
            FROM workout_replacement_votes
            WHERE request_id = ? AND vote = 'approve'
            """,
            (request_id,),
        ).fetchone()["count"]
        if active_count == 0 or approve_count < active_count:
            return False, ""

        plan = connection.execute(
            "SELECT * FROM weekly_plans WHERE id = ?",
            (request["original_weekly_plan_id"],),
        ).fetchone()
        if plan is None or plan["status"] not in EDITABLE_STATUSES:
            return False, "Pôvodný tréning už nemožno nahradiť."

        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "UPDATE workout_replacement_requests SET status = 'approved' WHERE id = ?",
            (request_id,),
        )
        connection.execute(
            "UPDATE weekly_plans SET status = 'replaced' WHERE id = ?",
            (plan["id"],),
        )
        connection.execute(
            """
            INSERT INTO weekly_plans (
                user_id, activity_version_id, week_start, workout_type, planned_day,
                planned_time, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'planned', ?)
            """,
            (
                plan["user_id"],
                request["replacement_activity_version_id"],
                replacement_week_start,
                request["replacement_workout_type"],
                request["replacement_day"],
                request["replacement_time"],
                now,
            ),
        )
        connection.execute(
            """
            UPDATE workout_replacement_requests
            SET status = 'applied', resolved_at = ?
            WHERE id = ?
            """,
            (now, request_id),
        )
    return True, f"Náhrada #{request_id} bola jednomyseľne schválená a aplikovaná."


def _owned_plan(connection, discord_user_id: str, plan_id: int):
    plan = connection.execute(
        """
        SELECT p.*, u.discord_user_id
        FROM weekly_plans p JOIN users u ON u.id = p.user_id
        WHERE p.id = ?
        """,
        (plan_id,),
    ).fetchone()
    if plan is None:
        return "Pôvodný tréning neexistuje."
    if plan["discord_user_id"] != discord_user_id:
        return "Tento tréning nepatrí tebe."
    return plan


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text).casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _pending_mentions(request_id: int) -> str:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT users.discord_user_id
            FROM users
            WHERE users.is_active = 1
              AND NOT EXISTS (
                  SELECT 1 FROM workout_replacement_votes votes
                  WHERE votes.request_id = ?
                    AND votes.voter_discord_user_id = users.discord_user_id
              )
            ORDER BY users.id
            """,
            (request_id,),
        ).fetchall()
    return " ".join(f"<@{row['discord_user_id']}>" for row in rows) or "nikto"
