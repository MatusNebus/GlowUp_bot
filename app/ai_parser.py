import json
from datetime import date

from openai import OpenAI

from app.config import OPENAI_API_KEY, OPENAI_MODEL


AI_NOT_CONFIGURED_MESSAGE = (
    "OpenAI API kľúč nie je nastavený. Prirodzený jazyk zatiaľ nefunguje."
)

SUPPORTED_INTENTS = [
    "plan_workout",
    "log_done",
    "log_short",
    "log_missed",
    "use_joker",
    "show_my_week",
    "show_week",
    "show_planning_status",
    "show_stats",
    "show_stats_all",
    "set_commitment",
    "ask_matus_decision",
    "forbidden_walk_replacement",
    "unknown",
]


def _nullable(value_type: str) -> dict:
    return {"anyOf": [{"type": value_type}, {"type": "null"}]}


INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": SUPPORTED_INTENTS},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "workout_type": _nullable("string"),
        "day": _nullable("string"),
        "time": _nullable("string"),
        "plan_id": _nullable("integer"),
        "result_text": _nullable("string"),
        "count_per_week": _nullable("integer"),
        "month": _nullable("string"),
        "target_user": _nullable("string"),
        "needs_matus_decision": {"type": "boolean"},
        "decision_question": _nullable("string"),
        "raw_summary": {"type": "string"},
    },
    "required": [
        "intent",
        "confidence",
        "workout_type",
        "day",
        "time",
        "plan_id",
        "result_text",
        "count_per_week",
        "month",
        "target_user",
        "needs_matus_decision",
        "decision_question",
        "raw_summary",
    ],
    "additionalProperties": False,
}

PARSER_INSTRUCTIONS = """
Si parser prirodzeného jazyka pre slovenského Discord fitness bota Jonáš.
Iba prelož správu na štruktúrovaný intent. Nikdy nevykonávaj databázové pravidlá.

Podporované významy:
- plan_workout: naplánovať tréning na deň a čas
- log_done, log_short, log_missed: zapísať výsledok konkrétneho plan_id
- use_joker: posunúť konkrétny plan_id
- show_my_week, show_week, show_planning_status
- show_stats, show_stats_all
- set_commitment
- forbidden_walk_replacement: používateľ chce nahradiť tréning prechádzkou/chôdzou/walk
- ask_matus_decision: nový alebo nejasný typ tréningu vyžaduje rozhodnutie Matúša
- unknown: nedostatok údajov alebo nejasný zámer

Normalizácia:
- dni: pondelok, utorok, streda, stvrtok, piatok, sobota, nedela
- čas vždy HH:MM, ak sa dá; "o šiestej večer" je 18:00
- "ráno" bez konkrétnej hodiny znamená time=null
- beh/behať je workout_type="beh"
- posilka/fitness je workout_type="posilka"
- domáci tréning je workout_type="domaci_trening"
- pri výsledku zachovaj užitočné údaje v result_text, napr. "5.2 km za 32 min"
- na vyriešenie odkazov vždy použi KONTEXT Z DATABÁZY:
  - "sobotný beh" je beh v sobotu
  - "dnešný tréning" je tréning označený DNES
  - "zajtrajší tréning" je tréning označený ZAJTRA
  - "posilka vo štvrtok" je posilka v deň stvrtok
  - "to, čo som chcel predtým" odkazuje na poslednú relevantnú predchádzajúcu správu
- ak aktuálna správa doplní plan_id a odkazuje na predchádzajúcu požiadavku,
  spoj plan_id s intentom, dňom, časom alebo výsledkom z poslednej relevantnej správy
- ak je priložená OTVORENÁ PENDING AKCIA a nová správa dopĺňa jej chýbajúce údaje,
  spoj pôvodný parsed JSON s novou správou a vráť kompletný pôvodný intent
- ak nová správa s pending akciou nesúvisí, pending akciu úplne ignoruj a parsuj
  novú správu samostatne
- ak opis jednoznačne zodpovedá práve jednému tréningu v pláne, nastav jeho plan_id
- ak opis zodpovedá viacerým tréningom, nastav intent="unknown", plan_id=null
  a v raw_summary jasne napíš, že existuje viac možných tréningov
- ak správa potrebuje ID a kontext ho nevie jednoznačne určiť, nechaj plan_id=null
- ak nový typ tréningu nie je jasne známy, použi ask_matus_decision,
  needs_matus_decision=true a napíš stručnú decision_question
- pri ostatných intentoch je needs_matus_decision=false
""".strip()


class OpenAIKeyMissingError(RuntimeError):
    pass


def parse_natural_message(
    message_text: str,
    author_display_name: str,
    context_text: str = "",
    pending_action_text: str = "",
) -> dict:
    """Preloží ľudskú správu na pevný JSON intent bez vykonania akcie."""
    if not OPENAI_API_KEY:
        raise OpenAIKeyMissingError(AI_NOT_CONFIGURED_MESSAGE)

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=PARSER_INSTRUCTIONS,
        input=(
            f"Dnešný dátum: {date.today().isoformat()}\n"
            f"Autor správy: {author_display_name}\n"
            f"Správa: {message_text}\n\n"
            f"{context_text}\n\n"
            f"{pending_action_text}"
        ),
        text={
            "format": {
                "type": "json_schema",
                "name": "couple_glowup_intent",
                "description": "Štruktúrovaný intent pre Couple GlowUp Bot.",
                "schema": INTENT_SCHEMA,
                "strict": True,
            }
        },
        max_output_tokens=700,
    )
    return json.loads(response.output_text)
