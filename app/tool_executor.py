import re
from datetime import datetime, timezone

from app.config import ADMIN_DISCORD_USER_ID
from app.database import get_connection
from app.services.activity_service import (
    create_activity,
    format_activities,
    request_activity_change,
    resolve_activity_change,
)
from app.services.capabilities_service import get_help
from app.services.rules_service import get_rules
from app.services.training_query_service import query_training_data
from app.services.commitment_change_service import list_changes, request_commitment_change, vote_change
from app.services.commitments_service import list_commitments
from app.services.joker_service import joker_status, use_joker
from app.services.onboarding_service import (
    confirm_onboarding,
    get_onboarding_status,
    process_onboarding_answer,
    reset_onboarding,
    start_onboarding,
)
from app.services.pending_actions_service import (
    create_pending_action,
    get_pending_action,
    get_latest_pending_action,
    resolve_pending_action,
)
from app.services.planning_service import (
    DAY_ORDER,
    add_plan,
    is_valid_time,
    list_all_week,
    list_my_week,
    normalize_day,
    resolve_plan_reference,
    weekly_status,
)
from app.services.replacement_service import (
    approve_replacement,
    get_replacement_detail,
    list_replacements,
    reject_replacement,
    request_workout_replacement,
)
from app.services.stats_service import get_user_month_stats
from app.services.workout_service import complete_workout, miss_workout, shorten_workout


def execute_tool(
    tool_name: str, args: dict, discord_user_id: str
) -> tuple[bool, str, str]:
    """Bezpečne vykoná agentom zvolený tool cez Python pravidlá a služby."""
    try:
        return _execute_tool(tool_name, args, discord_user_id)
    except (TypeError, ValueError) as error:
        return False, f"Chýbajú alebo nesedia údaje pre túto akciu: {error}", "user_error"
    except Exception as error:
        print(f"Tool executor chyba pri {tool_name}: {error}")
        return False, "Akciu sa nepodarilo vykonať. Skús to znova.", "user_error"


def _execute_tool(tool_name: str, args: dict, discord_user_id: str):
    if tool_name == "get_my_week":
        return _service_result(list_my_week(discord_user_id), "system_info")
    if tool_name == "get_group_week":
        return True, list_all_week(), "system_info"
    if tool_name == "get_planning_status":
        return _service_result(weekly_status(discord_user_id), "planning")
    if tool_name == "get_commitments":
        return True, _format_commitments(discord_user_id), "system_info"
    if tool_name == "get_stats":
        return _service_result(
            get_user_month_stats(discord_user_id, args.get("month")), "stats"
        )
    if tool_name == "plan_workout":
        return _service_result(
            add_plan(
                discord_user_id,
                _required(args, "workout_type"),
                _required(args, "day"),
                _required(args, "time"),
            ),
            "planning",
        )
    if tool_name == "move_workout":
        return _move_workout(discord_user_id, args)
    if tool_name == "delete_workout":
        return _delete_workout(discord_user_id, _required_int(args, "plan_ref"))
    if tool_name == "set_workout_status":
        return _set_workout_status(discord_user_id, args)
    if tool_name == "log_workout_done":
        return _workout_result(complete_workout, discord_user_id, args, "training_success")
    if tool_name == "log_workout_short":
        return _workout_result(shorten_workout, discord_user_id, args, "training_edit")
    if tool_name == "log_workout_missed":
        plan_id = _resolve_ref(discord_user_id, _required_int(args, "plan_ref"))
        return _service_result(miss_workout(discord_user_id, plan_id), "training_missed")
    if tool_name == "undo_last_action":
        pending = get_latest_pending_action(discord_user_id)
        if pending is None:
            return False, "Nie je otvorená žiadna bezpečne vratná akcia.", "user_error"
        resolve_pending_action(pending["id"])
        return True, "Posledná otvorená akcia bola zrušená.", "system_info"
    if tool_name == "use_joker":
        return _confirmed_joker_move(discord_user_id, args)
    if tool_name == "get_joker_status":
        return _service_result(joker_status(discord_user_id), "system_info")
    if tool_name == "request_commitment_change":
        return _service_result(
            request_commitment_change(
                discord_user_id,
                _required(args, "workout_type"),
                _required_int(args, "count_per_week"),
            ),
            "planning",
        )
    if tool_name in {"approve_commitment_change", "reject_commitment_change"}:
        vote = "approve" if tool_name.startswith("approve") else "reject"
        return _service_result(
            vote_change(discord_user_id, _required_int(args, "request_id"), vote),
            "planning",
        )
    if tool_name == "list_commitment_changes":
        return True, list_changes(), "system_info"
    if tool_name == "request_workout_replacement":
        return _service_result(
            request_workout_replacement(
                discord_user_id,
                args.get("plan_ref"),
                args.get("original_description"),
                _required(args, "workout_type"),
                _required(args, "day"),
                _required(args, "time"),
                _required(args, "reason"),
            ),
            "system_info",
        )
    if tool_name == "approve_replacement":
        return _service_result(
            approve_replacement(discord_user_id, _required_int(args, "request_id")),
            "system_info",
        )
    if tool_name == "reject_replacement":
        return _service_result(
            reject_replacement(discord_user_id, _required_int(args, "request_id")),
            "system_info",
        )
    if tool_name == "list_replacements":
        return True, list_replacements(), "system_info"
    if tool_name == "get_replacement_detail":
        return True, get_replacement_detail(_required_int(args, "request_id")), "system_info"
    if tool_name == "start_onboarding":
        return _service_result(start_onboarding(discord_user_id), "system_info")
    if tool_name == "continue_onboarding":
        answer = args.get("answer")
        result = (
            process_onboarding_answer(discord_user_id, answer)
            if answer
            else get_onboarding_status(discord_user_id)
        )
        return _service_result(result, "system_info")
    if tool_name == "confirm_onboarding":
        return _service_result(confirm_onboarding(discord_user_id), "planning")
    if tool_name == "reset_onboarding":
        return _service_result(reset_onboarding(discord_user_id), "system_info")
    if tool_name == "list_activity_types":
        return True, format_activities(), "system_info"
    if tool_name == "create_activity":
        return _create_activity_tool(discord_user_id, args)
    if tool_name == "request_activity_edit":
        return _service_result(
            request_activity_change(
                discord_user_id,
                _required(args, "activity_name"),
                "edit",
                args.get("new_activity_name"),
                args.get("activity_fields") or [],
            ),
            "system_info",
        )
    if tool_name == "request_activity_deactivation":
        return _service_result(
            request_activity_change(
                discord_user_id, _required(args, "activity_name"), "deactivate"
            ),
            "system_info",
        )
    if tool_name in {"approve_activity_change", "reject_activity_change"}:
        return _service_result(
            resolve_activity_change(
                discord_user_id,
                _required_int(args, "request_id"),
                tool_name.startswith("approve"),
            ),
            "system_info",
        )
    if tool_name == "query_training_data":
        return _service_result(
            query_training_data(discord_user_id, args.get("query") or {}),
            "stats",
        )
    if tool_name == "get_rules":
        return True, get_rules(), "system_info"
    if tool_name == "legacy_list_pending_decisions":
        return False, "Starý zoznam rozhodnutí už nie je podporovaný.", "user_error"
    if tool_name == "legacy_resolve_decision":
        return False, "Staré rozhodnutia už nie sú podporované.", "user_error"
        if not ADMIN_DISCORD_USER_ID or discord_user_id != ADMIN_DISCORD_USER_ID.strip():
            return False, "Toto rozhodnutie môže uzavrieť iba admin.", "user_error"
        decision_id = _required_int(args, "decision_id")
        decision = get_pending_action(decision_id)
        if (
            decision is None
            or decision["intent"] != "new_activity_decision"
            or decision["is_resolved"]
        ):
            return False, "Otvorené rozhodnutie s týmto ID neexistuje.", "user_error"
        answer = args.get("answer") or "rozhodnuté"
        if any(word in answer.casefold() for word in ("approve", "schvaľ", "súhlas", "povoľ")):
            workout_type = decision["parsed_json"].get("workout_type")
            if workout_type:
                with get_connection() as connection:
                    connection.execute(
                        """
                        INSERT INTO legacy_removed_activity_types (
                            workout_type, approved_by_discord_user_id, approved_at
                        )
                        VALUES (?, ?, ?)
                        ON CONFLICT(workout_type) DO UPDATE SET
                            approved_by_discord_user_id = excluded.approved_by_discord_user_id,
                            approved_at = excluded.approved_at
                        """,
                        (
                            workout_type,
                            discord_user_id,
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
        resolve_pending_action(decision_id)
        return True, f"Rozhodnutie #{decision_id} bolo uzavreté: {answer}.", "system_info"
    if tool_name == "show_help":
        return True, get_help(), "system_info"
    if tool_name == "answer_general_training_question":
        return True, args.get("question") or args.get("answer") or "", "general_advice"
    if tool_name == "casual_reply":
        return True, args.get("question") or args.get("answer") or "", "casual"
    if tool_name == "ask_clarifying_question":
        return True, args.get("question") or "Čo presne chceš spraviť?", "clarify"
    return False, f"Tool `{tool_name}` nie je podporovaný.", "user_error"


def _move_workout(discord_user_id: str, args: dict) -> tuple[bool, str, str]:
    plan_ref = _required_int(args, "plan_ref")
    new_day = normalize_day(_required(args, "day"))
    new_time = _required(args, "time")
    if not is_valid_time(new_time):
        return False, "Čas musí byť vo formáte HH:MM.", "user_error"
    plan_id = _resolve_ref(discord_user_id, plan_ref)

    with get_connection() as connection:
        plan = _owned_plan(connection, discord_user_id, plan_id)
        if isinstance(plan, str):
            return False, plan, "user_error"
        if plan["status"] not in {"planned", "postponed", "unanswered"}:
            return False, "Ukončený tréning sa už nedá presunúť.", "user_error"

        old_order = DAY_ORDER[plan["planned_day"]]
        new_order = DAY_ORDER[new_day]
        if new_order > old_order:
            if new_order - old_order > 1:
                return False, "Žolík môže posunúť tréning maximálne o jeden deň.", "user_error"
            joker = connection.execute(
                "SELECT id FROM jokers WHERE user_id = ? AND week_start = ?",
                (plan["user_id"], plan["week_start"]),
            ).fetchone()
            if joker is not None:
                return False, "Žolíka si už tento týždeň použil/a.", "user_error"
            parsed = {
                "tool": "use_joker",
                "args": {"plan_ref": plan_ref, "day": new_day, "time": new_time},
            }
            create_pending_action(
                discord_user_id,
                "confirm_joker_move",
                f"Presun tréningu {plan_ref} na {new_day} {new_time}",
                ["confirmation"],
                parsed,
            )
            return (
                True,
                "Presun na neskorší deň znamená použitie žolíka. Chceš ho naozaj "
                "minúť? Odpíš napríklad: áno, použi žolíka.",
                "clarify",
            )

        connection.execute(
            """
            UPDATE weekly_plans
            SET planned_day = ?, planned_time = ?, status = 'planned'
            WHERE id = ?
            """,
            (new_day, new_time, plan_id),
        )
    return True, f"Tréning [{plan_ref}] je presunutý na {new_day} {new_time}.", "planning"


def _create_activity_tool(discord_user_id: str, args: dict) -> tuple[bool, str, str]:
    pending = get_latest_pending_action(discord_user_id)
    name = args.get("activity_name")
    fields = args.get("activity_fields") or []
    if pending and pending["intent"] == "create_activity":
        name = name or pending["parsed_json"].get("activity_name")
        fields = fields or pending["parsed_json"].get("activity_fields") or []
    if not name or not fields:
        create_pending_action(
            discord_user_id,
            "create_activity",
            "Vytvorenie novej aktivity",
            [item for item, value in (("activity_name", name), ("activity_fields", fields)) if not value],
            {"activity_name": name, "activity_fields": fields},
        )
        return (
            False,
            "Napíš v jednej správe názov aktivity a všetky údaje, ktoré sa majú po tréningu zapisovať.",
            "clarify",
        )
    result = create_activity(discord_user_id, str(name), fields)
    if result[0] and pending and pending["intent"] == "create_activity":
        resolve_pending_action(pending["id"])
    return _service_result(result, "system_info")


def _confirmed_joker_move(discord_user_id: str, args: dict) -> tuple[bool, str, str]:
    pending = get_latest_pending_action(discord_user_id)
    joker_args = dict(args)
    if pending and pending["intent"] == "confirm_joker_move":
        joker_args = {**pending["parsed_json"].get("args", {}), **_without_none(args)}

    plan_id = _resolve_ref(discord_user_id, _required_int(joker_args, "plan_ref"))
    result = use_joker(
        discord_user_id,
        plan_id,
        _required(joker_args, "day"),
        _required(joker_args, "time"),
    )
    if result[0] and pending and pending["intent"] == "confirm_joker_move":
        resolve_pending_action(pending["id"])
    return _service_result(result, "joker")


def _delete_workout(discord_user_id: str, plan_ref: int) -> tuple[bool, str, str]:
    plan_id = _resolve_ref(discord_user_id, plan_ref)
    with get_connection() as connection:
        plan = _owned_plan(connection, discord_user_id, plan_id)
        if isinstance(plan, str):
            return False, plan, "user_error"
        if plan["status"] not in {"planned", "postponed", "unanswered"}:
            return False, "Ukončený tréning sa nedá vymazať.", "user_error"
        if plan["joker_used"]:
            return False, "Tréning s použitým žolíkom zatiaľ nemožno vymazať.", "user_error"
        connection.execute("DELETE FROM weekly_plans WHERE id = ?", (plan_id,))
    return True, f"Tréning [{plan_ref}] bol vymazaný z plánu.", "planning"


def _set_workout_status(discord_user_id: str, args: dict):
    status = _required(args, "status").casefold()
    if status == "missed":
        plan_id = _resolve_ref(discord_user_id, _required_int(args, "plan_ref"))
        return _service_result(miss_workout(discord_user_id, plan_id), "training_missed")
    return False, "Použi zápis splneného, skráteného alebo vynechaného tréningu.", "user_error"


def _workout_result(function, discord_user_id: str, args: dict, result_type: str):
    plan_id = _resolve_ref(discord_user_id, _required_int(args, "plan_ref"))
    pending = get_latest_pending_action(discord_user_id)
    result_values = args.get("result_values") or []
    if pending and pending["intent"] == "complete_workout_result":
        old_values = pending["parsed_json"].get("result_values") or []
        by_key = {item["field_key"]: item for item in old_values}
        by_key.update({item["field_key"]: item for item in result_values})
        result_values = list(by_key.values())
    result = result_values or _required(args, "result_text")
    service_result = function(discord_user_id, plan_id, result)
    if service_result[0]:
        if pending and pending["intent"] == "complete_workout_result":
            resolve_pending_action(pending["id"])
        return _service_result(service_result, result_type)
    if "Potrebujem všetky výsledky:" in service_result[1]:
        create_pending_action(
            discord_user_id,
            "complete_workout_result",
            service_result[1],
            ["result_values"],
            {
                "plan_ref": args.get("plan_ref"),
                "result_values": result_values,
                "tool": "log_workout_short" if result_type == "training_edit" else "log_workout_done",
            },
        )
        return False, service_result[1], "clarify"
    return _service_result(service_result, result_type)


def _edit_run_result(discord_user_id: str, args: dict):
    return False, "Použi dynamické parametre aktivity pri novom zápise.", "user_error"


def _edit_strength_result(discord_user_id: str, args: dict):
    return False, "Použi dynamické parametre aktivity pri novom zápise.", "user_error"


def _edit_note(discord_user_id: str, args: dict):
    return False, "Použi dynamické parametre aktivity pri novom zápise.", "user_error"


def _update_log(discord_user_id: str, plan_ref: int, values: dict):
    return False, "Úprava starého formátu výsledkov už nie je podporovaná.", "user_error"


def _owned_plan(connection, discord_user_id: str, plan_id: int):
    plan = connection.execute(
        """
        SELECT weekly_plans.*, users.discord_user_id
        FROM weekly_plans JOIN users ON users.id = weekly_plans.user_id
        WHERE weekly_plans.id = ?
        """,
        (plan_id,),
    ).fetchone()
    if plan is None:
        return "Taký tréning neexistuje."
    if plan["discord_user_id"] != discord_user_id:
        return "Tento tréning nepatrí tebe."
    return plan


def _resolve_ref(discord_user_id: str, plan_ref: int) -> int:
    success, result = resolve_plan_reference(discord_user_id, plan_ref)
    if not success:
        raise ValueError(result)
    return int(result)


def _format_commitments(discord_user_id: str) -> str:
    commitments = list_commitments(discord_user_id)
    if not commitments:
        return "Nemáš nastavené žiadne záväzky."
    return "Záväzky: " + ", ".join(
        f"{item['workout_type']} {item['count_per_week']}x" for item in commitments
    )


def _list_pending_decisions() -> str:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, original_message, created_at
            FROM pending_actions
            WHERE intent = 'new_activity_decision' AND is_resolved = 0
            ORDER BY id ASC
            """
        ).fetchall()
    if not rows:
        return "Nie sú otvorené žiadne rozhodnutia o nových aktivitách."
    return "\n".join(
        ["Otvorené rozhodnutia:"]
        + [f"#{row['id']} {row['original_message']}" for row in rows]
    )


def _service_result(result: tuple[bool, str], success_type: str):
    success, message = result
    return success, message, success_type if success else "user_error"


def _required(args: dict, key: str) -> str:
    value = args.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"chýba `{key}`")
    return str(value).strip()


def _required_int(args: dict, key: str) -> int:
    value = args.get(key)
    if value is None:
        raise ValueError(f"chýba `{key}`")
    return int(value)


def _without_none(values: dict) -> dict:
    return {key: value for key, value in values.items() if value is not None}
