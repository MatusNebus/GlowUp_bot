import asyncio
import unittest
from datetime import datetime, timezone
from pathlib import Path

import app.database as database
from app.services.activity_service import create_activity
from app.services.commitment_change_service import (
    change_commitment_type,
    request_commitment_change,
    vote_change,
)
from app.services.commitments_service import list_commitments, set_commitment
from app.services.planning_service import add_plan, get_week_start
from app.services.joker_service import use_joker
from app.services.users_service import ensure_user_exists
from app.services.scheduler_service import _users_missing_commitments
from app.services.context_service import build_ai_context, save_channel_message
from app.tool_executor import execute_tool
from app.bot import _natural_approval_result, send_and_remember


TEST_DB = Path(__file__).resolve().parent.parent / "data" / "test_core_flows.db"


class CoreFlowTests(unittest.TestCase):
    def setUp(self):
        if TEST_DB.exists():
            TEST_DB.unlink()
        database.DB_PATH = TEST_DB
        database.init_database()
        ensure_user_exists("1", "Matúš")
        ensure_user_exists("2", "Ema")
        fields = [
            {
                "field_key": "duration",
                "display_name": "čas",
                "field_type": "duration",
                "unit": "min",
            }
        ]
        create_activity("1", "beh", fields)
        create_activity("1", "fitko", fields)

    def tearDown(self):
        if TEST_DB.exists():
            TEST_DB.unlink()

    def test_auto_registration_is_idempotent(self):
        created, user = ensure_user_exists("3", "Nina")
        created_again, _ = ensure_user_exists("3", "Nina Nová")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(user["onboarding_state"], "needs_commitments")

    def test_increase_is_immediate_and_decrease_is_unanimous(self):
        set_commitment("1", "beh", 2)
        success, _ = request_commitment_change("1", "beh", 3)
        self.assertTrue(success)
        self.assertEqual(list_commitments("1")[0]["count_per_week"], 3)

        success, message = request_commitment_change("1", "beh", 2)
        self.assertTrue(success)
        self.assertIn("<@2>", message)
        self.assertEqual(list_commitments("1")[0]["count_per_week"], 3)
        success, _ = vote_change("2", 1, "approve")
        self.assertTrue(success)
        self.assertEqual(list_commitments("1")[0]["count_per_week"], 2)

    def test_activity_type_swap_keeps_weekly_count(self):
        set_commitment("1", "beh", 2)
        success, _ = change_commitment_type("1", "beh", "fitko", 2)
        self.assertTrue(success)
        commitments = list_commitments("1")
        self.assertEqual([(item["workout_type"], item["count_per_week"]) for item in commitments], [("fitko", 2)])

    def test_next_week_planning(self):
        set_commitment("1", "beh", 2)
        success, _ = add_plan("1", "beh", "pondelok", "18:00", "next_week")
        self.assertTrue(success)
        with database.get_connection() as connection:
            row = connection.execute("SELECT week_start FROM weekly_plans").fetchone()
        self.assertEqual(row["week_start"], get_week_start("next_week"))

    def test_week_planning_pending_keeps_target_week(self):
        set_commitment("1", "beh", 1)
        self.assertTrue(
            execute_tool("start_week_planning", {"target_week": "next_week"}, "1")[0]
        )
        self.assertTrue(
            execute_tool(
                "plan_workout",
                {
                    "workout_type": "beh",
                    "day": "utorok",
                    "time": "18:00",
                    "target_week": None,
                },
                "1",
            )[0]
        )
        with database.get_connection() as connection:
            row = connection.execute("SELECT week_start FROM weekly_plans").fetchone()
        self.assertEqual(row["week_start"], get_week_start("next_week"))

    def test_joker_can_move_sunday_to_next_monday(self):
        set_commitment("1", "beh", 1)
        self.assertTrue(add_plan("1", "beh", "nedela", "18:00")[0])
        with database.get_connection() as connection:
            plan = connection.execute("SELECT id, week_start FROM weekly_plans").fetchone()
        self.assertTrue(use_joker("1", plan["id"], "pondelok", "20:00")[0])
        with database.get_connection() as connection:
            moved = connection.execute(
                "SELECT week_start, planned_day, status FROM weekly_plans"
            ).fetchone()
        self.assertEqual(moved["week_start"], get_week_start("next_week"))
        self.assertEqual(moved["planned_day"], "pondelok")
        self.assertEqual(moved["status"], "postponed")

    def test_missing_activity_commitment_requests_fields_then_resumes(self):
        success, _, result_type = execute_tool(
            "save_commitments",
            {
                "commitments": [{"activity_name": "plávanie", "count_per_week": 2}]
            },
            "2",
        )
        self.assertFalse(success)
        self.assertEqual(result_type, "clarify")
        success, _, _ = execute_tool(
            "create_activity_with_fields",
            {
                "activity_name": None,
                "activity_fields": [
                    {
                        "field_key": "distance",
                        "display_name": "vzdialenosť",
                        "field_type": "number",
                        "unit": "m",
                    }
                ],
            },
            "2",
        )
        self.assertTrue(success)
        self.assertEqual(list_commitments("2")[0]["count_per_week"], 2)

    def test_commitment_reminder_selection_uses_two_hour_cooldown(self):
        now = datetime.now(timezone.utc)
        self.assertIn("2", [user["discord_user_id"] for user in _users_missing_commitments(now)])
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE users SET last_commitment_reminder_at = ? WHERE discord_user_id = '2'",
                (now.isoformat(),),
            )
        self.assertNotIn("2", [user["discord_user_id"] for user in _users_missing_commitments(now)])

    def test_context_contains_human_and_bot_messages_in_order(self):
        save_channel_message("1", "Matúš", "channel-1", "prvá správa")
        save_channel_message("jonas", "Jonáš", "channel-1", "moja otázka", is_bot=True)
        save_channel_message("1", "Matúš", "channel-1", "kliky číslo, zhyby číslo")
        context = build_ai_context("1", "channel-1", 5)
        self.assertIn("RECENT CHANNEL MESSAGES:", context)
        self.assertLess(context.index("prvá správa"), context.index("moja otázka"))
        self.assertLess(context.index("moja otázka"), context.index("kliky číslo"))
        with database.get_connection() as connection:
            bot_row = connection.execute(
                "SELECT is_bot FROM message_memory WHERE discord_user_id = 'jonas'"
            ).fetchone()
        self.assertEqual(bot_row["is_bot"], 1)

    def test_natural_approval_uses_single_open_request(self):
        set_commitment("1", "beh", 3)
        self.assertTrue(request_commitment_change("1", "beh", 2)[0])
        result = _natural_approval_result("2", "schvaľujem")
        self.assertIsNotNone(result)
        self.assertTrue(result[0])
        self.assertEqual(list_commitments("1")[0]["count_per_week"], 2)

    def test_send_and_remember_stores_bot_reply_and_hides_internal_details(self):
        class FakeChannel:
            id = "channel-send"

            def __init__(self):
                self.sent = []

            async def send(self, content):
                self.sent.append(content)
                return content

        channel = FakeChannel()
        asyncio.run(send_and_remember(channel, "pending action week_planning plan_slots"))
        self.assertNotIn("pending action", channel.sent[0])
        with database.get_connection() as connection:
            row = connection.execute(
                """
                SELECT author_id, author_name, is_bot, channel_id, content, timestamp
                FROM message_memory WHERE channel_id = 'channel-send'
                """
            ).fetchone()
        self.assertEqual(row["author_name"], "Jonáš")
        self.assertEqual(row["is_bot"], 1)
        self.assertTrue(row["timestamp"])


if __name__ == "__main__":
    unittest.main()
