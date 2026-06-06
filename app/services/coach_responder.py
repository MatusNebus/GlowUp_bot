from openai import OpenAI

from app.config import OPENAI_API_KEY, OPENAI_MODEL


COACH_INSTRUCTIONS = """
Si Jonáš, tréner v Couple GlowUp Bot. Odpovedaj po slovensky.
Tón je podporujúci, ale prísny, praktický a občas jemne vtipný, nikdy cringe.
Odpoveď má typicky 1 až 4 krátke vety.
Píš iba krátky trénerovský komentár. Neopakuj faktický výsledok, aplikácia ho
zobrazí samostatne pred tvojím komentárom. Pri general_advice, casual_chat,
app_help a context_question však odpovedz priamo na požiadavku alebo response hint,
pretože žiadny samostatný výsledok sa nezobrazí.

Nemôžeš meniť ani obchádzať systémové pravidlá:
- prechádzka nikdy nenahrádza povinný tréning,
- žolík je iba raz týždenne a posúva tréning maximálne o jeden deň,
- nedeľa nemení počet tréningov, iba harmonogram,
- zmena záväzku mimo onboardingu a dev režimu potrebuje súhlas všetkých aktívnych používateľov,
- vynechania sa vždy zapisujú,
- pri vynechaní buď priamy a prísny, bez urážok,
- pri splnení pochváľ stručne,
- nepridávaj fakty, ktoré nie sú vo faktickom výsledku,
- pri zdravotnej otázke alebo zranení nedávaj diagnózu a odporuč odborníka.

Pri jedle po tréningu môžeš občas pripomenúť, že tréning nie je poukážka
na prejedanie, ale neopakuj túto vetu stále.
""".strip()


def generate_coach_reply(
    event_type: str, factual_result: str, user_context: str | None = None
) -> str:
    """Vytvorí prirodzenú trénerovskú odpoveď alebo použije bezpečný fallback."""
    fallback = _fallback_for_event(event_type, factual_result)
    if not OPENAI_API_KEY:
        return fallback

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=COACH_INSTRUCTIONS,
            input=(
                f"Typ udalosti: {event_type}\n"
                f"Faktický výsledok, ktorý nesmieš zmeniť: {factual_result}\n"
                f"Stručný kontext používateľa: {user_context or 'bez kontextu'}"
            ),
            max_output_tokens=250,
        )
        reply = response.output_text.strip()
        if not reply:
            return fallback
        if event_type in {"general_advice", "casual_chat", "app_help", "context_question"}:
            return reply
        return f"{factual_result}\n\n{reply}"
    except Exception as error:
        print(f"OpenAI coach responder chyba: {error}")
        return fallback


def respond_success(action_result: str, context: dict | None = None) -> str:
    """Pochváli splnenú akciu stručne a bez prehnaného oslavovania."""
    response = f"{action_result}\n\nDobrá práca."
    if "tréning" in action_result.casefold():
        response += (
            "\nTréning nie je poukážka na prejedanie. Daj si normálne jedlo, "
            "vodu a neznehodnoť dnešnú snahu."
        )
    return response


def respond_shortened(action_result: str, context: dict | None = None) -> str:
    """Uzná skrátený tréning, ale pomenuje rozdiel oproti plnému výkonu."""
    return (
        f"{action_result}\n\n"
        "Snaha sa ráta, ale skrátený tréning nie je plný výkon. "
        "Nabudúce dokončíme celý plán."
    )


def respond_missed(action_result: str, context: dict | None = None) -> str:
    """Pomenuje vynechanie priamo, bez urážania používateľa."""
    return (
        f"{action_result}\n\n"
        "Plán nebol návrh, bol to záväzok. Jeden výpadok nie je koniec sveta, "
        "ale opakovanie z toho spraví zvyk."
    )


def respond_joker(action_result: str, context: dict | None = None) -> str:
    """Pripomenie, že žolík posúva termín, nie samotný záväzok."""
    return (
        f"{action_result}\n\n"
        "Férový odklad. Toto nie je reset záväzku. V nový termín sa to plní."
    )


def respond_error(error_message: str, context: dict | None = None) -> str:
    """Vráti chybu prirodzene a navedie používateľa na ďalší krok."""
    return f"{error_message}\n\nSkús to opraviť a ideme ďalej. Ticho nie je stratégia."


def respond_unknown(message: str | None = None) -> str:
    """Dopýta sa pri nejasnej požiadavke."""
    detail = message or (
        "Rozumiem, že niečo chceš, ale nemám dosť údajov. "
        "Doplň typ tréningu, deň, čas alebo ID tréningu."
    )
    return f"{detail}Povedz mi konkrétne, čo chceš spraviť."


def respond_forbidden_walk() -> str:
    """Tvrdo odmietne prechádzku ako náhradu povinného tréningu."""
    return (
        "Nie. Prechádzka sa podľa pravidiel Couple GlowUp neráta ako tréning "
        "a nemôže nahradiť plánovaný beh ani posilku. Môže byť bonus alebo "
        "regenerácia, ale povinný tréning ostáva. Plán nebol návrh, bol to záväzok."
    )


def respond_planning_success(action_result: str) -> str:
    """Potvrdí plánovanie a pripomenie, že rozhodujúce bude vykonanie."""
    return (
        f"{action_result}\n\n"
        "Plán je zapísaný. Keď príde termín: Dnes je tréningový deň. "
        "Stačí začať. Nemusíš byť motivovaný, stačí byť obutý."
    )


def respond_stats(action_result: str) -> str:
    """Doplní štatistiky o stručnú trénerovskú poznámku."""
    return (
        f"{action_result}\n\n"
        "Čísla nevyjednávajú. Ukazujú, čo sa naozaj stalo."
    )


def _fallback_for_event(event_type: str, factual_result: str) -> str:
    if event_type in {"general_advice", "casual_chat", "app_help", "context_question"}:
        return (
            factual_result
            if factual_result
            else "Povedz mi to trochu konkrétnejšie a nájdeme praktický ďalší krok."
        )

    responders = {
        "success": respond_success,
        "shortened": respond_shortened,
        "missed": respond_missed,
        "joker": respond_joker,
        "planning": respond_planning_success,
        "stats": respond_stats,
        "error": respond_error,
        "forbidden_walk": lambda _: respond_forbidden_walk(),
        "onboarding": respond_success,
    }
    responder = responders.get(event_type, respond_unknown)
    return responder(factual_result)
