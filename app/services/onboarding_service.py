import re
from datetime import datetime, timezone

from app.database import get_connection
from app.services.commitments_service import set_commitment


QUESTIONS = {
    "goal": (
        "Aký je tvoj hlavný cieľ? Kondícia, sila, chudnutie, pravidelnosť, "
        "výbušnosť alebo kombinácia?"
    ),
    "level": (
        "Aká je tvoja aktuálna úroveň? Začiatočník, mierne pokročilý alebo pokročilý?"
    ),
    "preferred_activities": (
        "Aké aktivity chceš robiť? Beh, posilka, domáci tréning, bicykel, "
        "plávanie, beachvolejbal..."
    ),
    "limitations": "Máš nejaké zranenia alebo obmedzenia? Ak nie, napíš: nemám.",
    "weekly_capacity": "Koľko tréningov týždenne realisticky zvládneš?",
}
FIELDS = tuple(QUESTIONS)
ACTIVITY_ALIASES = {
    "beh": ("beh", "beha"),
    "posilka": ("posilka", "fitness", "sil"),
    "domaci_trening": ("domáci", "domaci"),
    "bicykel": ("bicykel", "bike"),
    "plavanie": ("plávanie", "plavanie"),
    "beachvolejbal": ("beachvolejbal", "beach", "volejbal"),
}


def start_onboarding(discord_user_id: str) -> tuple[bool, str]:
    user = _get_user(discord_user_id)
    if user is None:
        return False, "Najprv sa zaregistruj cez: jonas register <meno>"

    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO user_profiles (user_id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                goal = NULL,
                level = NULL,
                preferred_activities = NULL,
                limitations = NULL,
                weekly_capacity = NULL,
                updated_at = excluded.updated_at
            """,
            (user["id"], now, now),
        )
    return True, QUESTIONS["goal"]


def get_onboarding_status(discord_user_id: str) -> tuple[bool, str]:
    user, profile = _get_user_and_profile(discord_user_id)
    if user is None:
        return False, "Najprv sa zaregistruj cez: jonas register <meno>"
    if profile is None:
        return True, "Onboarding ešte nebol spustený. Použi: jonas onboarding start"

    missing = _next_missing_field(profile)
    if missing:
        return True, f"Onboarding pokračuje.\n{QUESTIONS[missing]}"

    return True, _format_summary(user["display_name"], profile)


def reset_onboarding(discord_user_id: str) -> tuple[bool, str]:
    user = _get_user(discord_user_id)
    if user is None:
        return False, "Najprv sa zaregistruj cez: jonas register <meno>"
    with get_connection() as connection:
        connection.execute("DELETE FROM user_profiles WHERE user_id = ?", (user["id"],))
    return True, "Onboarding profil bol vymazaný. Začni cez: jonas onboarding start"


def has_active_onboarding(discord_user_id: str) -> bool:
    _, profile = _get_user_and_profile(discord_user_id)
    return profile is not None and _next_missing_field(profile) is not None


def process_onboarding_answer(
    discord_user_id: str, answer: str
) -> tuple[bool, str]:
    user, profile = _get_user_and_profile(discord_user_id)
    if user is None or profile is None:
        return False, "Onboarding nie je spustený. Použi: jonas onboarding start"

    field = _next_missing_field(profile)
    if field is None:
        return True, _format_summary(user["display_name"], profile)

    clean_answer = answer.strip()
    if not clean_answer:
        return False, QUESTIONS[field]

    with get_connection() as connection:
        connection.execute(
            f"UPDATE user_profiles SET {field} = ?, updated_at = ? WHERE user_id = ?",
            (clean_answer, datetime.now(timezone.utc).isoformat(), user["id"]),
        )

    _, updated_profile = _get_user_and_profile(discord_user_id)
    next_field = _next_missing_field(updated_profile)
    if next_field:
        return True, QUESTIONS[next_field]
    return True, _format_summary(user["display_name"], updated_profile)


def confirm_onboarding(discord_user_id: str) -> tuple[bool, str]:
    user, profile = _get_user_and_profile(discord_user_id)
    if user is None or profile is None:
        return False, "Onboarding nie je pripravený na potvrdenie."
    if _next_missing_field(profile):
        return False, "Onboarding ešte nie je dokončený. Použi: jonas onboarding status"

    proposal = _build_commitment_proposal(profile)
    if not proposal:
        return False, "Neviem vytvoriť návrh commitments. Uprav onboarding aktivity."

    messages = []
    for workout_type, count in proposal.items():
        success, message = set_commitment(discord_user_id, workout_type, count)
        if not success:
            return False, message
        messages.append(f"{workout_type} {count}x")

    return True, "Commitments potvrdené: " + ", ".join(messages)


def _format_summary(display_name: str, profile) -> str:
    proposal = _build_commitment_proposal(profile)
    proposal_text = ", ".join(f"{key} {value}x" for key, value in proposal.items())
    return (
        f"Onboarding dokončený pre {display_name}.\n"
        f"Cieľ: {profile['goal']}\n"
        f"Úroveň: {profile['level']}\n"
        f"Aktivity: {profile['preferred_activities']}\n"
        f"Obmedzenia: {profile['limitations']}\n"
        f"Kapacita: {profile['weekly_capacity']}\n"
        f"Návrh commitments: {proposal_text or 'bez návrhu'}\n"
        "Ak súhlasíš, použi: jonas onboarding confirm"
    )


def _build_commitment_proposal(profile) -> dict[str, int]:
    activity_text = (profile["preferred_activities"] or "").casefold()
    activities = [
        workout_type
        for workout_type, aliases in ACTIVITY_ALIASES.items()
        if any(alias in activity_text for alias in aliases)
    ]
    capacity_match = re.search(r"\d+", profile["weekly_capacity"] or "")
    capacity = int(capacity_match.group()) if capacity_match else 0
    if not activities or capacity <= 0:
        return {}

    selected = activities[:capacity]
    proposal = {activity: 1 for activity in selected}
    remaining = capacity - len(selected)
    index = 0
    while remaining > 0:
        activity = selected[index % len(selected)]
        proposal[activity] += 1
        remaining -= 1
        index += 1
    return proposal


def _next_missing_field(profile) -> str | None:
    return next((field for field in FIELDS if not profile[field]), None)


def _get_user(discord_user_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT id, display_name FROM users WHERE discord_user_id = ? AND is_active = 1",
            (discord_user_id,),
        ).fetchone()


def _get_user_and_profile(discord_user_id: str):
    user = _get_user(discord_user_id)
    if user is None:
        return None, None
    with get_connection() as connection:
        profile = connection.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?", (user["id"],)
        ).fetchone()
    return user, profile
