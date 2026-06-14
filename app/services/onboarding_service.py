import json
import re
import unicodedata
from datetime import datetime, timezone

from app.database import get_connection
from app.services.commitments_service import set_commitment
from app.services.activity_service import list_activities


WELCOME_MESSAGE = """Vitaj v Couple GlowUp.

Funguje to jednoducho:

1. Najprv si nastavíš týždenný záväzok, napríklad 2x beh a 2x posilka.
2. Tento počet sa počas nedeľného plánovania neznižuje.
3. V nedeľu večer si len vyberieš presné dni a časy tréningov.
4. Po tréningu zapíšeš výsledok.
5. Každý má 1 žolíka týždenne na posun tréningu maximálne o 1 deň.
6. Prechádzka sa neráta ako tréning.

Napíš mi jednou vetou, aké tréningy chceš mať a koľkokrát týždenne.
Napríklad: chcem 2x beh a 2x posilku."""

UNCLEAR_MESSAGE = (
    "Potrebujem konkrétny týždenný záväzok. Napíš napríklad: "
    "chcem 2x beh a 2x posilku. Ak chceš úplný základ, odporúčam "
    "2x beh a 1x domáci tréning."
)

def start_onboarding(discord_user_id: str) -> tuple[bool, str]:
    """Spustí krátky onboarding a vymaže starý nepotvrdený návrh."""
    if _get_user(discord_user_id) is None:
        return False, "Najprv sa zaregistruj cez: jonas register <meno>"

    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO onboarding_sessions (
                discord_user_id, last_answer, proposed_commitments,
                is_active, created_at, updated_at
            )
            VALUES (?, NULL, NULL, 1, ?, ?)
            ON CONFLICT(discord_user_id) DO UPDATE SET
                last_answer = NULL,
                proposed_commitments = NULL,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (discord_user_id, now, now),
        )
    return True, WELCOME_MESSAGE


def process_onboarding_answer(
    discord_user_id: str, answer: str
) -> tuple[bool, str]:
    """Z jednej vety vytvorí návrh týždenných commitments."""
    session = _get_session(discord_user_id)
    if session is None or not session["is_active"]:
        return False, "Onboarding nie je spustený. Použi: jonas onboarding start"

    clean_answer = answer.strip()
    proposal = parse_commitments(clean_answer)
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE onboarding_sessions
            SET last_answer = ?, proposed_commitments = ?, updated_at = ?
            WHERE discord_user_id = ?
            """,
            (
                clean_answer,
                json.dumps(proposal, ensure_ascii=False) if proposal else None,
                now,
                discord_user_id,
            ),
        )

    if not proposal:
        return False, UNCLEAR_MESSAGE
    return True, _format_proposal(proposal)


def confirm_onboarding(discord_user_id: str) -> tuple[bool, str]:
    """Po potvrdení vytvorí navrhnuté commitments priamo."""
    session = _get_session(discord_user_id)
    if session is None or not session["is_active"]:
        return False, "Onboarding nie je aktívny. Použi: jonas onboarding start"

    proposal = _load_proposal(session["proposed_commitments"])
    if not proposal:
        return False, UNCLEAR_MESSAGE

    messages = []
    for workout_type, count in proposal.items():
        success, message = set_commitment(discord_user_id, workout_type, count)
        if not success:
            return False, message
        messages.append(f"{workout_type} {count}x")

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE onboarding_sessions
            SET is_active = 0, updated_at = ?
            WHERE discord_user_id = ?
            """,
            (datetime.now(timezone.utc).isoformat(), discord_user_id),
        )
    return True, "Týždenný záväzok je potvrdený: " + ", ".join(messages) + "."


def get_onboarding_status(discord_user_id: str) -> tuple[bool, str]:
    """Debug stav ukazuje iba aktivitu, poslednú odpoveď a návrh."""
    if _get_user(discord_user_id) is None:
        return False, "Najprv sa zaregistruj cez: jonas register <meno>"

    session = _get_session(discord_user_id)
    if session is None:
        return True, "Onboarding aktívny: nie\nPosledná odpoveď: žiadna\nNávrh: žiadny"

    proposal = _load_proposal(session["proposed_commitments"])
    proposal_text = ", ".join(f"{key} {value}x" for key, value in proposal.items())
    return (
        True,
        f"Onboarding aktívny: {'áno' if session['is_active'] else 'nie'}\n"
        f"Posledná odpoveď: {session['last_answer'] or 'žiadna'}\n"
        f"Návrh: {proposal_text or 'žiadny'}",
    )


def reset_onboarding(discord_user_id: str) -> tuple[bool, str]:
    if _get_user(discord_user_id) is None:
        return False, "Najprv sa zaregistruj cez: jonas register <meno>"
    with get_connection() as connection:
        connection.execute(
            "DELETE FROM onboarding_sessions WHERE discord_user_id = ?",
            (discord_user_id,),
        )
    return True, "Onboarding bol resetovaný. Začni cez: jonas onboarding start"


def has_active_onboarding(discord_user_id: str) -> bool:
    session = _get_session(discord_user_id)
    return session is not None and bool(session["is_active"])


def parse_commitments(answer: str) -> dict[str, int]:
    """Parse counts only for activities currently present in the catalog."""
    normalized = _normalize(answer).replace("×", "x")
    number_matches = list(re.finditer(r"\b(\d+)\s*x?\b", normalized))
    if not number_matches:
        return {}

    proposal = {}
    used_numbers: set[int] = set()
    for activity in list_activities():
        workout_type = activity["display_name"]
        aliases = (_normalize(activity["display_name"]), _normalize(activity["slug"]))
        alias_matches = [
            match
            for alias in aliases
            for match in re.finditer(rf"\b{re.escape(alias)}\b", normalized)
        ]
        if not alias_matches:
            continue

        activity_match = min(alias_matches, key=lambda match: match.start())
        candidates = []
        for index, number_match in enumerate(number_matches):
            if index in used_numbers:
                continue
            distance = min(
                abs(activity_match.start() - number_match.end()),
                abs(number_match.start() - activity_match.end()),
            )
            if distance <= 25:
                candidates.append((distance, index, number_match))
        if not candidates:
            continue

        _, number_index, number_match = min(candidates, key=lambda item: item[0])
        count = int(number_match.group(1))
        if count > 0:
            proposal[workout_type] = count
            used_numbers.add(number_index)
    return proposal


def _format_proposal(proposal: dict[str, int]) -> str:
    lines = ["Navrhujem týždenný záväzok:", ""]
    lines.extend(f"- {workout_type} {count}x" for workout_type, count in proposal.items())
    lines.extend(
        [
            "",
            "Ak súhlasíš, napíš: jonas onboarding confirm",
            "",
            "V nedeľu o 19:00 ti potom napíšem a vyberieš presné dni a časy "
            "tréningov na ďalší týždeň.",
        ]
    )
    return "\n".join(lines)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _load_proposal(raw_proposal: str | None) -> dict[str, int]:
    if not raw_proposal:
        return {}
    return {key: int(value) for key, value in json.loads(raw_proposal).items()}


def _get_user(discord_user_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT id FROM users WHERE discord_user_id = ? AND is_active = 1",
            (discord_user_id,),
        ).fetchone()


def _get_session(discord_user_id: str):
    with get_connection() as connection:
        return connection.execute(
            "SELECT * FROM onboarding_sessions WHERE discord_user_id = ?",
            (discord_user_id,),
        ).fetchone()
