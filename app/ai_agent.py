import json
import logging
from datetime import date

from openai import OpenAI

from app.config import OPENAI_API_KEY, OPENAI_MODEL
from app.services.rules_service import get_rules

logger = logging.getLogger(__name__)
AI_NOT_CONFIGURED_MESSAGE = "AI odpovede teraz nie sú dostupné. Skús to znova neskôr."


class OpenAIKeyMissingError(RuntimeError):
    pass


TOOLS = [
    "get_my_week",
    "show_week_plan",
    "get_group_week",
    "get_planning_status",
    "get_commitments",
    "get_stats",
    "plan_workout",
    "start_week_planning",
    "move_workout",
    "delete_workout",
    "set_workout_status",
    "log_workout_done",
    "log_workout_short",
    "log_workout_missed",
    "undo_last_action",
    "use_joker",
    "get_joker_status",
    "request_commitment_change",
    "change_commitment",
    "start_commitment_change_approval",
    "vote_commitment_change",
    "approve_commitment_change",
    "reject_commitment_change",
    "list_commitment_changes",
    "request_workout_replacement",
    "vote_workout_replacement",
    "approve_replacement",
    "reject_replacement",
    "list_replacements",
    "get_replacement_detail",
    "start_onboarding",
    "save_commitments",
    "continue_onboarding",
    "confirm_onboarding",
    "reset_onboarding",
    "list_activity_types",
    "create_activity",
    "create_activity_with_fields",
    "ask_for_activity_fields",
    "request_activity_edit",
    "request_activity_deactivation",
    "approve_activity_change",
    "reject_activity_change",
    "query_training_data",
    "get_rules",
    "show_help",
    "answer_general_training_question",
    "casual_reply",
    "ask_clarifying_question",
    "log_workout",
    "reply_only",
]


def _nullable(value_type: str) -> dict:
    return {"anyOf": [{"type": value_type}, {"type": "null"}]}


ACTIVITY_FIELDS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "field_key": {"type": "string"},
            "display_name": {"type": "string"},
            "field_type": {"type": "string", "enum": ["number", "duration", "text", "rating"]},
            "unit": _nullable("string"),
        },
        "required": ["field_key", "display_name", "field_type", "unit"],
        "additionalProperties": False,
    },
}

RESULT_VALUES_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"field_key": {"type": "string"}, "value": {"type": "string"}},
        "required": ["field_key", "value"],
        "additionalProperties": False,
    },
}

COMMITMENTS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "activity_name": {"type": "string"},
            "count_per_week": {"type": "integer", "minimum": 1},
        },
        "required": ["activity_name", "count_per_week"],
        "additionalProperties": False,
    },
}

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "scope": {"type": "string", "enum": ["self", "group"]},
        "activity": _nullable("string"),
        "date_from": _nullable("string"),
        "date_to": _nullable("string"),
        "statuses": {"type": "array", "items": {"type": "string"}},
        "field_key": _nullable("string"),
        "aggregation": {"type": "string", "enum": ["count", "sum", "average", "min", "max"]},
    },
    "required": ["scope", "activity", "date_from", "date_to", "statuses", "field_key", "aggregation"],
    "additionalProperties": False,
}


ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "plan_ref": _nullable("integer"),
        "workout_type": _nullable("string"),
        "day": _nullable("string"),
        "time": _nullable("string"),
        "result_text": _nullable("string"),
        "status": _nullable("string"),
        "month": _nullable("string"),
        "count_per_week": _nullable("integer"),
        "request_id": _nullable("integer"),
        "decision_id": _nullable("integer"),
        "answer": _nullable("string"),
        "note": _nullable("string"),
        "question": _nullable("string"),
        "original_description": _nullable("string"),
        "reason": _nullable("string"),
        "activity_name": _nullable("string"),
        "new_activity_name": _nullable("string"),
        "old_activity_name": _nullable("string"),
        "target_week": _nullable("string"),
        "vote": _nullable("string"),
        "activity_fields": ACTIVITY_FIELDS_SCHEMA,
        "commitments": COMMITMENTS_SCHEMA,
        "result_values": RESULT_VALUES_SCHEMA,
        "query": QUERY_SCHEMA,
    },
    "required": [
        "plan_ref",
        "workout_type",
        "day",
        "time",
        "result_text",
        "status",
        "month",
        "count_per_week",
        "request_id",
        "decision_id",
        "answer",
        "note",
        "question",
        "original_description",
        "reason",
        "activity_name",
        "new_activity_name",
        "old_activity_name",
        "target_week",
        "vote",
        "activity_fields",
        "commitments",
        "result_values",
        "query",
    ],
    "additionalProperties": False,
}

AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["tool", "reply", "clarify"]},
        "tool": {"anyOf": [{"type": "string", "enum": TOOLS}, {"type": "null"}]},
        "args": ARGS_SCHEMA,
        "reply_intent": _nullable("string"),
        "clarification_question": _nullable("string"),
        "tone": {
            "type": "string",
            "enum": ["neutral", "coach", "strict", "supportive", "casual"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason_summary": {"type": "string"},
    },
    "required": [
        "mode",
        "tool",
        "args",
        "reply_intent",
        "clarification_question",
        "tone",
        "confidence",
        "reason_summary",
    ],
    "additionalProperties": False,
}

AGENT_INSTRUCTIONS = f"""
Si rozhodovacia vrstva slovenského Discord trénera Jonáš. Vyber presne jeden tool,
priamu odpoveď alebo doplňujúcu otázku. Nikdy nemeníš databázu sám.

Podporované tools:
{", ".join(TOOLS)}

Rozhodovacie pravidlá:
- Používaj používateľské čísla tréningov 1..n z kontextu, nikdy interné_id.
- Objektívnu náhradu tréningu rieš cez request_workout_replacement. Agent iba vytvorí
  request; nikdy ju sám neschvaľuje.
- Pri request_workout_replacement vyplň plan_ref, ak vieš jednoznačne nájsť pôvodný
  tréning. Inak vyplň original_description. Vždy vyplň workout_type, day, time a reason.
- Ak pri náhrade chýba nový tréning, deň alebo čas, mode=clarify a spýtaj sa:
  "Aký tréning chceš dať ako náhradu a kedy?"
- "schvaľujem tú náhradu" použi approve_replacement a request_id z kontextu, iba ak
  je otvorená práve jedna náhrada. Analogicky reject_replacement.
- "ukáž otvorené náhrady" je list_replacements.
- Zmenu záväzku rieš cez request_commitment_change.
- Zvýšenie alebo zníženie počtu rieš cez change_commitment. Python rozhodne, či treba hlasovanie.
- Zmenu typu záväzku bez zníženia počtu rieš cez change_commitment s old_activity_name,
  new_activity_name a count_per_week.
- Pri používateľovi bez záväzkov extrahuj všetky aktivity a počty do save_commitments.
  Ak aktivita neexistuje, Python si vypýta jej používateľom definované polia.
- Počas automatického onboardingu používaj save_commitments, nie starý continue_onboarding.
- Žiadosť o plánovanie tohto alebo budúceho týždňa rieš cez start_week_planning a target_week.
- Pri plan_workout nastav target_week podľa konverzácie; bez údaja použi current_week.
- Ak je otvorená pending akcia week_planning, pri ďalších plan_workout použi jej target_week.
- Ak je otvorená pending akcia create_activity, save_commitments, replacement_activity
  alebo commitment_type_activity, aktuálna krátka správa opisuje polia aktivity.
  Prelož napríklad "kliky číslo, zhyby číslo" na activity_fields a vyber
  create_activity_with_fields. Nikdy takúto odpoveď nevyhodnoť ako náhradu tréningu.
- Presun na rovnaký alebo skorší deň rieš move_workout.
- Presun na neskorší deň tiež začni cez move_workout; Python vyžiada potvrdenie žolíka.
- Ak kontext obsahuje pending confirm_joker_move a používateľ jasne potvrdí,
  vyber use_joker a prevezmi plan_ref, deň a čas z pending akcie.
- Ak potvrdenie odmietne, zvoľ reply s reply_intent=cancel_pending.
- Pri otázke o tréningu, výžive alebo motivácii vyber answer_general_training_question.
- Pri bežnom rozhovore vyber casual_reply.
- Pri otázke ako aplikáciu používať vyber show_help.
- Pri otázke na pravidlá vždy vyber get_rules.
- Pri otázke na známe aktivity vyber list_activity_types.
- Novú aktivitu vždy rieš cez create_activity, aj keď chýba názov alebo polia.
  Polia mapuj na number, duration, text alebo rating. Python uloží rozpracovanie
  a jednou otázkou si vypýta názov aj všetky chýbajúce parametre.
- Úpravu aktivity rieš request_activity_edit, odstránenie request_activity_deactivation.
  Pri úprave parametrov pošli kompletnú požadovanú novú schému, nie iba zmenené pole.
  Adminov súhlas rieš approve_activity_change alebo reject_activity_change.
- Pri zápise výsledku použi result_values podľa field_key z katalógu v kontexte.
  Trvanie normalizuj na číselnú hodnotu v jednotke uvedenej pri poli.
  Ak je otvorená pending akcia complete_workout_result, doplň chýbajúce result_values
  a znova vyber pôvodný tool zápisu výsledku.
- Otázky na ľubovoľné súčty, priemery, minimum, maximum alebo počty tréningových dát
  rieš cez query_training_data. Nikdy nevytváraj SQL.
- Ak chýbajú nutné údaje alebo je viac možností, mode=clarify.
- Systémové informácie majú neutral tón. Splnenie môže byť supportive, vynechanie strict.
- Dni normalizuj na pondelok, utorok, streda, stvrtok, piatok, sobota, nedela.
- Čas používaj HH:MM.
""".strip()


def decide_agent_action(
    message_text: str,
    author_display_name: str,
    context_text: str,
    pending_action_text: str = "",
) -> dict:
    """Vráti bezpečné štruktúrované rozhodnutie bez vykonania toolu."""
    if not OPENAI_API_KEY:
        raise OpenAIKeyMissingError(AI_NOT_CONFIGURED_MESSAGE)

    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            instructions=AGENT_INSTRUCTIONS,
            input=(
                f"Dátum: {date.today().isoformat()}\n"
                f"Autor: {author_display_name}\n"
                f"Aktuálna správa: {message_text}\n\n"
                f"{context_text}\n\n{pending_action_text}\n\nAKTUÁLNE PRAVIDLÁ:\n{get_rules()}"
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "couple_glowup_agent_decision",
                    "schema": AGENT_SCHEMA,
                    "strict": True,
                }
            },
            max_output_tokens=800,
        )
        decision = json.loads(response.output_text)
        logger.debug(
            "AI agent decision mode=%s tool=%s args=%s reply_intent=%s json_parse=success",
            decision.get("mode"),
            decision.get("tool"),
            decision.get("args"),
            decision.get("reply_intent"),
        )
        return decision
    except Exception:
        logger.exception("AI agent failed json_parse=error")
        raise
