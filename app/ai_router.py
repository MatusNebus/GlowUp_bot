import json
from datetime import date

from openai import OpenAI

from app.ai_parser import AI_NOT_CONFIGURED_MESSAGE, OpenAIKeyMissingError, SUPPORTED_INTENTS
from app.config import OPENAI_API_KEY, OPENAI_MODEL


ROUTES = [
    "tool_action",
    "general_advice",
    "casual_chat",
    "app_help",
    "context_question",
    "commitment_change_request",
    "unknown",
]


def _nullable(value_type: str) -> dict:
    return {"anyOf": [{"type": value_type}, {"type": "null"}]}


TOOL_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "workout_type": _nullable("string"),
        "day": _nullable("string"),
        "time": _nullable("string"),
        "plan_id": _nullable("integer"),
        "result_text": _nullable("string"),
        "count_per_week": _nullable("integer"),
        "month": _nullable("string"),
        "target_user": _nullable("string"),
        "request_id": _nullable("integer"),
        "vote": _nullable("string"),
    },
    "required": [
        "workout_type",
        "day",
        "time",
        "plan_id",
        "result_text",
        "count_per_week",
        "month",
        "target_user",
        "request_id",
        "vote",
    ],
    "additionalProperties": False,
}

ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "route": {"type": "string", "enum": ROUTES},
        "intent": {"anyOf": [{"type": "string", "enum": SUPPORTED_INTENTS}, {"type": "null"}]},
        "tool_args": TOOL_ARGS_SCHEMA,
        "needs_clarification": {"type": "boolean"},
        "clarification_question": _nullable("string"),
        "response_hint": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "route",
        "intent",
        "tool_args",
        "needs_clarification",
        "clarification_question",
        "response_hint",
        "confidence",
    ],
    "additionalProperties": False,
}

ROUTER_INSTRUCTIONS = """
Si AI router slovenského fitness bota Jonáš. Iba klasifikuj a extrahuj údaje.
Nikdy nemeníš databázu a nevymýšľaš splnené tréningy.

Route:
- tool_action: plánovanie, zápis výsledku, žolík, výpis plánu/štatistík
- commitment_change_request: používateľ chce zmeniť počet existujúceho záväzku
- general_advice: otázka o tréningu, výžive, motivácii alebo cvičení
- casual_chat: bežná konverzácia, reakcia alebo pochvala
- app_help: návod, čo bot vie alebo ako sa používa
- context_question: otázka na posledné správy alebo konverzáciu
- unknown: nič z uvedeného

Príklady:
- "čo je najlepšie na výbušnosť?" -> general_advice
- "super, takéto hlášky sa mi páčia" -> casual_chat
- "daj mi návod ako aplikáciu používať" -> app_help
- "čo som napísal v poslednej správe?" -> context_question; odpoveď nájdi v kontexte
- "chcem zmeniť beh z 2x na 3x" -> commitment_change_request, intent=set_commitment
- "schvaľujem zmenu 4" -> commitment_change_request, request_id=4, vote=approve
- "odmietam zmenu 4" -> commitment_change_request, request_id=4, vote=reject
- "v piatok 18:00 beh" -> tool_action, intent=plan_workout

Pri odkaze na tréning použi plán z kontextu. Do plan_id vlož používateľské číslo,
nie interné_id. Pri viacerých možnostiach nastav needs_clarification=true.
Dni normalizuj: pondelok, utorok, streda, stvrtok, piatok, sobota, nedela.
Čas vracaj ako HH:MM. Beh=beh, posilka=posilka, domáci tréning=domaci_trening.
Prechádzka ako náhrada je tool_action s intent=forbidden_walk_replacement.
response_hint stručne zhrnie, čo má finálna odpoveď povedať; pri context_question
v ňom uveď presnú odpoveď podľa posledných správ.
""".strip()


def route_natural_message(
    message_text: str,
    author_display_name: str,
    context_text: str = "",
    pending_action_text: str = "",
) -> dict:
    """Rozdelí prirodzenú správu na rozhovor, radu alebo vykonateľnú akciu."""
    if not OPENAI_API_KEY:
        raise OpenAIKeyMissingError(AI_NOT_CONFIGURED_MESSAGE)

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=ROUTER_INSTRUCTIONS,
        input=(
            f"Dátum: {date.today().isoformat()}\n"
            f"Autor: {author_display_name}\n"
            f"Správa: {message_text}\n\n{context_text}\n\n{pending_action_text}"
        ),
        text={
            "format": {
                "type": "json_schema",
                "name": "couple_glowup_router",
                "schema": ROUTER_SCHEMA,
                "strict": True,
            }
        },
        max_output_tokens=700,
    )
    return json.loads(response.output_text)
