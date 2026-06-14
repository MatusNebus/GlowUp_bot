from app.database import get_connection


def reset_me(discord_user_id: str) -> tuple[bool, str]:
    """Vymaže všetky používateľské dáta autora."""
    with get_connection() as connection:
        user = connection.execute(
            "SELECT id, display_name FROM users WHERE discord_user_id = ?",
            (discord_user_id,),
        ).fetchone()
        if user is None:
            return False, "Tento Discord používateľ nemá uložené dáta."

        _delete_user_data(connection, user["id"], discord_user_id)

    return True, f"Dáta používateľa {user['display_name']} boli zmazané."


def reset_user(display_name: str) -> tuple[bool, str]:
    """Vymaže dáta používateľa podľa display_name."""
    with get_connection() as connection:
        users = connection.execute(
            "SELECT id, discord_user_id, display_name FROM users"
        ).fetchall()
        user = next(
            (
                row
                for row in users
                if row["display_name"].strip().casefold()
                == display_name.strip().casefold()
            ),
            None,
        )
        if user is None:
            return False, f"Používateľ {display_name} neexistuje."

        _delete_user_data(connection, user["id"], user["discord_user_id"])

    return True, f"Dáta používateľa {user['display_name']} boli zmazané."


def reset_all() -> tuple[bool, str]:
    """Vymaže všetky používateľské dáta projektu."""
    with get_connection() as connection:
        for table in (
            "workout_log_values",
            "workout_replacement_votes",
            "workout_replacement_requests",
            "commitment_change_votes",
            "commitment_change_requests",
            "workout_logs",
            "jokers",
            "weekly_plans",
            "commitments",
            "onboarding_sessions",
            "user_profiles",
            "message_memory",
            "pending_actions",
            "notification_log",
            "activity_change_requests",
            "activity_fields",
            "activity_versions",
            "activity_types",
            "users",
        ):
            connection.execute(f"DELETE FROM {table}")

    return True, "Všetky používateľské dáta boli zmazané."


def _delete_user_data(connection, user_id: int, discord_user_id: str) -> None:
    replacement_ids = connection.execute(
        """
        SELECT id FROM workout_replacement_requests
        WHERE requester_discord_user_id = ?
           OR original_weekly_plan_id IN (
               SELECT id FROM weekly_plans WHERE user_id = ?
           )
        """,
        (discord_user_id, user_id),
    ).fetchall()
    for request in replacement_ids:
        connection.execute(
            "DELETE FROM workout_replacement_votes WHERE request_id = ?",
            (request["id"],),
        )
    connection.execute(
        """
        DELETE FROM workout_replacement_requests
        WHERE requester_discord_user_id = ?
           OR original_weekly_plan_id IN (
               SELECT id FROM weekly_plans WHERE user_id = ?
           )
        """,
        (discord_user_id, user_id),
    )
    connection.execute(
        "DELETE FROM workout_replacement_votes WHERE voter_discord_user_id = ?",
        (discord_user_id,),
    )
    request_ids = connection.execute(
        """
        SELECT id FROM commitment_change_requests
        WHERE requester_discord_user_id = ? OR target_discord_user_id = ?
        """,
        (discord_user_id, discord_user_id),
    ).fetchall()
    for request in request_ids:
        connection.execute(
            "DELETE FROM commitment_change_votes WHERE request_id = ?", (request["id"],)
        )
    connection.execute(
        """
        DELETE FROM commitment_change_requests
        WHERE requester_discord_user_id = ? OR target_discord_user_id = ?
        """,
        (discord_user_id, discord_user_id),
    )
    connection.execute(
        "DELETE FROM commitment_change_votes WHERE voter_discord_user_id = ?",
        (discord_user_id,),
    )
    connection.execute(
        """
        DELETE FROM workout_log_values
        WHERE workout_log_id IN (SELECT id FROM workout_logs WHERE user_id = ?)
        """,
        (user_id,),
    )
    connection.execute("DELETE FROM workout_logs WHERE user_id = ?", (user_id,))
    connection.execute("DELETE FROM jokers WHERE user_id = ?", (user_id,))
    connection.execute("DELETE FROM weekly_plans WHERE user_id = ?", (user_id,))
    connection.execute("DELETE FROM commitments WHERE user_id = ?", (user_id,))
    connection.execute(
        "DELETE FROM onboarding_sessions WHERE discord_user_id = ?", (discord_user_id,)
    )
    connection.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
    connection.execute(
        "DELETE FROM message_memory WHERE discord_user_id = ?", (discord_user_id,)
    )
    connection.execute(
        "DELETE FROM pending_actions WHERE discord_user_id = ?", (discord_user_id,)
    )
    connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
