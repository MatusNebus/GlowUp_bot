import json
from datetime import date

from openai import OpenAI

from app.ai_parser import AI_NOT_CONFIGURED_MESSAGE, OpenAIKeyMissingError
from app.config import OPENAI_API_KEY, OPENAI_MODEL


TOOLS = [
    "get_my_week",
    "get_group_week",
    "get_planning_status",
    "get_commitments",
    "get_stats",
    "plan_workout",
    "move_workout",
    "delete_workout",
    "set_workout_status",
    "log_workout_done",
    "log_workout_short",
    "log_workout_missed",
    "edit_run_result",
    "edit_strength_result",
    "edit_workout_note",
    "undo_last_action",
    "use_joker",
    "get_joker_status",
    "request_commitment_change",
    "approve_commitment_change",
    "reject_commitment_change",
    "list_commitment_changes",
    "request_workout_replacement",
    "approve_replacement",
    "reject_replacement",
    "list_replacements",
    "get_replacement_detail",
    "start_onboarding",
    "continue_onboarding",
    "confirm_onboarding",
    "reset_onboarding",
    "list_activity_types",
    "request_new_activity_decision",
    "list_pending_decisions",
    "resolve_decision",
    "show_help",
    "answer_general_training_question",
    "casual_reply",
    "ask_clarifying_question",
]


def _nullable(value_type: str) -> dict:
    return {"anyOf": [{"type": value_type}, {"type": "null"}]}


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

Pravidlá:
- Používaj používateľské čísla tréningov 1..n z kontextu, nikdy interné_id.
- Prechádzka nie je náhrada tréningu.
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
- Presun na rovnaký alebo skorší deň rieš move_workout.
- Presun na neskorší deň tiež začni cez move_workout; Python vyžiada potvrdenie žolíka.
- Ak kontext obsahuje pending confirm_joker_move a používateľ jasne potvrdí,
  vyber use_joker a prevezmi plan_ref, deň a čas z pending akcie.
- Ak potvrdenie odmietne, zvoľ reply s reply_intent=cancel_pending.
- Pri otázke o tréningu, výžive alebo motivácii vyber answer_general_training_question.
- Pri bežnom rozhovore vyber casual_reply.
- Pri otázke ako aplikáciu používať vyber show_help.
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
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=AGENT_INSTRUCTIONS,
        input=(
            f"Dátum: {date.today().isoformat()}\n"
            f"Autor: {author_display_name}\n"
            f"Aktuálna správa: {message_text}\n\n"
            f"{context_text}\n\n{pending_action_text}"
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
    return json.loads(response.output_text)
