from datetime import datetime, timezone

from app.database import get_connection
from app.services.commitments_service import set_commitment
from app.services.activity_service import get_active_activity


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
        activity = get_active_activity(workout_type, connection)
        if activity is None:
            return False, f"Aktivita `{workout_type}` nie je v aktívnom katalógu."
        normalized_type = activity["display_name"]
        user = connection.execute(
            """
            SELECT users.id, users.display_name, commitments.count_per_week
            FROM users
            LEFT JOIN commitments
              ON commitments.user_id = users.id
             AND commitments.activity_type_id = ?
             AND commitments.is_active = 1
            WHERE users.discord_user_id = ? AND users.is_active = 1
            """,
            (activity["id"], requester_discord_user_id),
        ).fetchone()
        if user is None:
            return False, "Najprv sa musíš registrovať."
        if user["count_per_week"] is None:
            return set_commitment(requester_discord_user_id, normalized_type, new_count)
        if user["count_per_week"] == new_count:
            return False, "Takýto záväzok už máš nastavený."
        if new_count > user["count_per_week"]:
            return set_commitment(requester_discord_user_id, normalized_type, new_count)

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
        f"Zmena prejde až po jednomyseľnom súhlase. Hlasujú: {_pending_mentions(request_id)}. "
        f"Odpovedzte `Jony, súhlasím so zmenou {request_id}` alebo "
        f"`Jony, nesúhlasím so zmenou {request_id}`.",
    )


def change_commitment_type(
    discord_user_id: str, old_workout_type: str, new_workout_type: str, count: int
) -> tuple[bool, str]:
    """Swap an activity without approval when the weekly total does not decrease."""
    if count <= 0:
        return False, "Počet tréningov musí byť väčší ako 0."
    with get_connection() as connection:
        user = connection.execute(
            "SELECT id, display_name FROM users WHERE discord_user_id = ? AND is_active = 1",
            (discord_user_id,),
        ).fetchone()
        old_activity = get_active_activity(old_workout_type, connection)
        new_activity = get_active_activity(new_workout_type, connection)
        if user is None:
            return False, "Používateľ nie je aktívny."
        if old_activity is None:
            return False, f"Aktivita `{old_workout_type}` nie je v aktívnom katalógu."
        if new_activity is None:
            return False, (
                f"Aktivita `{new_workout_type}` ešte neexistuje. "
                "Najprv zadaj parametre, ktoré sa pri nej majú zapisovať."
            )
        current = connection.execute(
            """
            SELECT id, count_per_week FROM commitments
            WHERE user_id = ? AND activity_type_id = ? AND is_active = 1
            """,
            (user["id"], old_activity["id"]),
        ).fetchone()
        if current is None:
            return False, f"Nemáš aktívny záväzok `{old_workout_type}`."
        if count < current["count_per_week"]:
            return False, "Zmena typu nesmie znížiť počet tréningov bez hlasovania."
        target = connection.execute(
            """
            SELECT id, count_per_week FROM commitments
            WHERE user_id = ? AND activity_type_id = ? AND is_active = 1
            """,
            (user["id"], new_activity["id"]),
        ).fetchone()
        connection.execute(
            "UPDATE commitments SET is_active = 0 WHERE id = ?", (current["id"],)
        )
        if target:
            connection.execute(
                "UPDATE commitments SET count_per_week = ? WHERE id = ?",
                (target["count_per_week"] + count, target["id"]),
            )
        else:
            connection.execute(
                """
                INSERT INTO commitments (
                    user_id, activity_type_id, workout_type, count_per_week, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    new_activity["id"],
                    new_activity["display_name"],
                    count,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
    return True, (
        f"{user['display_name']} zmenil/a záväzok {old_activity['display_name']} "
        f"{current['count_per_week']}x na {new_activity['display_name']} {count}x."
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


def _pending_mentions(request_id: int) -> str:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT users.discord_user_id
            FROM users
            WHERE users.is_active = 1
              AND NOT EXISTS (
                  SELECT 1 FROM commitment_change_votes votes
                  WHERE votes.request_id = ?
                    AND votes.voter_discord_user_id = users.discord_user_id
              )
            ORDER BY users.id
            """,
            (request_id,),
        ).fetchall()
    return " ".join(f"<@{row['discord_user_id']}>" for row in rows) or "nikto"
