import logging

from openai import OpenAI

from app.config import OPENAI_API_KEY, OPENAI_MODEL
from app.services.rules_service import get_rules

logger = logging.getLogger(__name__)

FINAL_REPLY_INSTRUCTIONS = """
Si Jonáš, prirodzený slovenský AI tréner v Discord chate.
Vytvor finálnu odpoveď z faktického výsledku Python toolu. Fakty ani pravidlá nemeň.
Discord mentiony vo formáte <@ID> zachovaj iba pri result_type=scheduled_reminder.
Pri všetkých ostatných typoch odpovedí mentiony nepoužívaj; nahraď ich prirodzeným
oslovením bez pingnutia.

Štýl:
- system_info a user_error: stručne, neutrálne,
- training_success: krátko podporujúco, ale bez preháňania,
- training_missed: priamo a prísne, bez urážok,
- scheduled_reminder: krátko trénerovsky a konkrétne,
- onboarding a scheduled_reminder: jemne, ale dôsledne; nikdy agresívne,
- planning, training_edit a joker: vecne a prirodzene,
- general_advice: praktická trénerovská odpoveď postavena na faktoch a realnych zdrojoch (zdroje nechcem aby si citoval),
- casual: prirodzená krátka konverzácia,
- typicky 1 až 5 krátkych viet,
- vtipny styl, moze byt kludne sarkazmus/ironia, ale nie cringe

""".strip()


def generate_final_reply(
    original_message: str,
    factual_result: str,
    result_type: str,
    tone: str = "neutral",
    user_context: str | None = None,
) -> str:
    """Vytvorí prirodzenú finálnu odpoveď; pri chybe API vráti faktický výsledok."""
    fallback = _fallback(result_type, factual_result)
    if not OPENAI_API_KEY:
        return fallback

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=FINAL_REPLY_INSTRUCTIONS + "\n\nAktuálne pravidlá:\n" + get_rules(),
            input=(
                f"Pôvodná správa: {original_message}\n"
                f"Faktický výsledok: {factual_result}\n"
                f"Typ výsledku: {result_type}\n"
                f"Požadovaný tón: {tone}\n"
                f"Stručný kontext: {user_context or 'bez ďalšieho kontextu'}"
            ),
            max_output_tokens=300,
        )
        return response.output_text.strip() or fallback
    except Exception:
        logger.exception("Coach responder failed; using factual fallback")
        return fallback


def generate_coach_reply(
    event_type: str, factual_result: str, user_context: str | None = None
) -> str:
    """Spätná kompatibilita pre existujúce volania."""
    result_types = {
        "success": "training_success",
        "shortened": "training_edit",
        "missed": "training_missed",
        "joker": "joker",
        "planning": "planning",
        "stats": "stats",
        "error": "user_error",
        "forbidden_walk": "user_error",
        "general_advice": "general_advice",
        "casual_chat": "casual",
        "app_help": "system_info",
        "context_question": "system_info",
    }
    return generate_final_reply(
        factual_result,
        factual_result,
        result_types.get(event_type, "system_info"),
        user_context=user_context,
    )


# Tvrdé príkazy používajú neutrálne fallbacky bez zbytočnej motivácie.
def respond_success(action_result: str, context: dict | None = None) -> str:
    return action_result


def respond_shortened(action_result: str, context: dict | None = None) -> str:
    return action_result


def respond_missed(action_result: str, context: dict | None = None) -> str:
    return action_result


def respond_joker(action_result: str, context: dict | None = None) -> str:
    return action_result


def respond_error(error_message: str, context: dict | None = None) -> str:
    return error_message


def respond_unknown(message: str | None = None) -> str:
    return message or "Potrebujem presnejšie údaje."


def respond_forbidden_walk() -> str:
    return (
        "Prechádzka sa podľa pravidiel Couple GlowUp neráta ako tréning a nemôže "
        "nahradiť plánovaný tréning."
    )


def respond_planning_success(action_result: str) -> str:
    return action_result


def respond_stats(action_result: str) -> str:
    return action_result


def _fallback(result_type: str, factual_result: str) -> str:
    if factual_result:
        return factual_result
    if result_type == "general_advice":
        return "Napíš otázku trochu konkrétnejšie, aby som ti vedel prakticky poradiť."
    if result_type == "casual":
        return "Rozumiem."
    return "Akciu sa nepodarilo dokončiť."
