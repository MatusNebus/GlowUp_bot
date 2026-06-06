from datetime import datetime, timezone

from app.database import get_connection
from app.services.commitments_service import set_commitment


def request_commitment_change(
    requester_discord_user_id: str,
    workout_type: str,
    new_count: int,
) -> tuple[bool, str]:
    """Vytvorí návrh zmeny existujúceho záväzku a hlas žiadateľa."""
    normalized_type = workout_type.strip().casefold()
    if new_count <= 0:
        return False, "Počet tréningov musí byť väčší ako 0."

    with get_connection() as connection:
        user = connection.execute(
            """
            SELECT users.id, users.display_name, commitments.count_per_week
            FROM users
            LEFT JOIN commitments
              ON commitments.user_id = users.id
             AND commitments.workout_type = ?
             AND commitments.is_active = 1
            WHERE users.discord_user_id = ? AND users.is_active = 1
            """,
            (normalized_type, requester_discord_user_id),
        ).fetchone()
        if user is None:
            return False, "Najprv sa musíš registrovať."
        if user["count_per_week"] is None:
            return set_commitment(requester_discord_user_id, normalized_type, new_count)
        if user["count_per_week"] == new_count:
            return False, "Takýto záväzok už máš nastavený."

        existing = connection.execute(
            """
            SELECT id FROM commitment_change_requests
            WHERE target_discord_user_id = ? AND workout_type = ? AND status = 'open'
            """,
            (requester_discord_user_id, normalized_type),
        ).fetchone()
        if existing:
            return False, f"Pre túto zmenu už existuje otvorený návrh #{existing['id']}."

        now = datetime.now(timezone.utc).isoformat()
        cursor = connection.execute(
            """
            INSERT INTO commitment_change_requests (
                requester_discord_user_id, target_discord_user_id, workout_type,
                old_count, new_count, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, 'open', ?)
            """,
            (
                requester_discord_user_id,
                requester_discord_user_id,
                normalized_type,
                user["count_per_week"],
                new_count,
                now,
            ),
        )
        request_id = cursor.lastrowid
        connection.execute(
            """
            INSERT INTO commitment_change_votes (
                request_id, voter_discord_user_id, vote, created_at
            )
            VALUES (?, ?, 'approve', ?)
            """,
            (request_id, requester_discord_user_id, now),
        )

    approved, approval_message = _apply_if_unanimous(request_id)
    if approved:
        return True, approval_message
    return (
        True,
        f"Návrh zmeny záväzku #{request_id}: {user['display_name']} chce zmeniť "
        f"{normalized_type} z {user['count_per_week']}x na {new_count}x týždenne. "
        "Zmena prejde až po súhlase všetkých aktívnych používateľov.",
    )


def vote_change(
    voter_discord_user_id: str, request_id: int, vote: str
) -> tuple[bool, str]:
    """Zapíše hlas aktívneho používateľa a prípadne uzavrie návrh."""
    normalized_vote = vote.strip().casefold()
    if normalized_vote not in {"approve", "reject"}:
        return False, "Hlas musí byť approve alebo reject."

    with get_connection() as connection:
        voter = connection.execute(
            "SELECT display_name FROM users WHERE discord_user_id = ? AND is_active = 1",
            (voter_discord_user_id,),
        ).fetchone()
        request = connection.execute(
            "SELECT * FROM commitment_change_requests WHERE id = ? AND status = 'open'",
            (request_id,),
        ).fetchone()
        if voter is None:
            return False, "Hlasovať môže iba aktívny registrovaný používateľ."
        if request is None:
            return False, "Otvorený návrh s týmto ID neexistuje."

        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO commitment_change_votes (
                request_id, voter_discord_user_id, vote, created_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(request_id, voter_discord_user_id)
            DO UPDATE SET vote = excluded.vote, created_at = excluded.created_at
            """,
            (request_id, voter_discord_user_id, normalized_vote, now),
        )
        if normalized_vote == "reject":
            connection.execute(
                """
                UPDATE commitment_change_requests
                SET status = 'rejected', resolved_at = ?
                WHERE id = ?
                """,
                (now, request_id),
            )
            return True, f"{voter['display_name']} odmietol/a návrh #{request_id}. Zmena neprešla."

    approved, message = _apply_if_unanimous(request_id)
    if approved:
        return True, message
    return True, f"Hlas používateľa {voter['display_name']} pre návrh #{request_id} je zapísaný."


def list_changes() -> str:
    """Vypíše otvorené návrhy a aktuálne hlasy."""
    with get_connection() as connection:
        requests = connection.execute(
            """
            SELECT r.*, users.display_name
            FROM commitment_change_requests r
            LEFT JOIN users ON users.discord_user_id = r.target_discord_user_id
            WHERE r.status = 'open'
            ORDER BY r.id ASC
            """
        ).fetchall()
        if not requests:
            return "Nie sú otvorené žiadne návrhy zmien záväzkov."

        lines = ["Otvorené zmeny záväzkov:"]
        for request in requests:
            votes = connection.execute(
                """
                SELECT vote, COUNT(*) AS count
                FROM commitment_change_votes WHERE request_id = ? GROUP BY vote
                """,
                (request["id"],),
            ).fetchall()
            vote_counts = {row["vote"]: row["count"] for row in votes}
            lines.append(
                f"#{request['id']} {request['display_name'] or request['target_discord_user_id']}: "
                f"{request['workout_type']} {request['old_count']}x -> {request['new_count']}x; "
                f"súhlas {vote_counts.get('approve', 0)}, nesúhlas {vote_counts.get('reject', 0)}"
            )
    return "\n".join(lines)


def _apply_if_unanimous(request_id: int) -> tuple[bool, str]:
    with get_connection() as connection:
        request = connection.execute(
            "SELECT * FROM commitment_change_requests WHERE id = ? AND status = 'open'",
            (request_id,),
        ).fetchone()
        if request is None:
            return False, ""
        active_count = connection.execute(
            "SELECT COUNT(*) AS count FROM users WHERE is_active = 1"
        ).fetchone()["count"]
        approve_count = connection.execute(
            """
            SELECT COUNT(*) AS count FROM commitment_change_votes
            WHERE request_id = ? AND vote = 'approve'
            """,
            (request_id,),
        ).fetchone()["count"]
        if active_count == 0 or approve_count < active_count:
            return False, ""

    success, result = set_commitment(
        request["target_discord_user_id"],
        request["workout_type"],
        request["new_count"],
    )
    if not success:
        return False, result
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE commitment_change_requests
            SET status = 'approved', resolved_at = ?
            WHERE id = ?
            """,
            (datetime.now(timezone.utc).isoformat(), request_id),
        )
    return True, f"Návrh #{request_id} schválili všetci aktívni používatelia. {result}"
