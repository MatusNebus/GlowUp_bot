import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import app.database as database
import app.services.activity_service as activity_service
from app.services.activity_service import (
    create_activity,
    list_activities,
    request_activity_change,
    resolve_activity_change,
)
from app.services.commitments_service import set_commitment
from app.services.planning_service import add_plan
from app.services.rules_service import get_rules
from app.services.training_query_service import query_training_data
from app.services.workout_service import complete_workout
from app.services.pending_actions_service import get_latest_pending_action
from app.tool_executor import execute_tool


class DynamicActivitiesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=".")
        self.original_path = database.DB_PATH
        self.original_admin = activity_service.ADMIN_DISCORD_USER_ID
        database.DB_PATH = Path(self.temp.name) / "test.db"
        database.init_database()
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO users (discord_user_id, display_name, created_at)
                VALUES ('user-1', 'Tester', ?)
                """,
                (datetime.now(timezone.utc).isoformat(),),
            )

    def tearDown(self):
        database.DB_PATH = self.original_path
        activity_service.ADMIN_DISCORD_USER_ID = self.original_admin
        self.temp.cleanup()

    def test_catalog_starts_empty_and_activity_can_be_created(self):
        self.assertEqual([], list_activities())
        success, _ = create_activity(
            "user-1",
            "Run",
            [
                {"display_name": "Distance", "field_type": "number", "unit": "km"},
                {"display_name": "Time", "field_type": "duration", "unit": "min"},
            ],
        )
        self.assertTrue(success)
        self.assertEqual(["Run"], [item["display_name"] for item in list_activities()])

    def test_incomplete_activity_tool_is_saved_and_completed(self):
        success, _, result_type = execute_tool(
            "create_activity", {"activity_name": "Run", "activity_fields": []}, "user-1"
        )
        self.assertFalse(success)
        self.assertEqual("clarify", result_type)
        self.assertEqual("create_activity", get_latest_pending_action("user-1")["intent"])
        success, _, _ = execute_tool(
            "create_activity",
            {
                "activity_name": None,
                "activity_fields": [
                    {"display_name": "Distance", "field_type": "number", "unit": "km"}
                ],
            },
            "user-1",
        )
        self.assertTrue(success)
        self.assertIsNone(get_latest_pending_action("user-1"))

    def test_dynamic_result_and_read_only_aggregation(self):
        create_activity(
            "user-1",
            "Run",
            [
                {"display_name": "Distance", "field_type": "number", "unit": "km"},
                {"display_name": "Feeling", "field_type": "rating"},
            ],
        )
        self.assertTrue(set_commitment("user-1", "run", 1)[0])
        self.assertTrue(add_plan("user-1", "run", "pondelok", "18:00")[0])
        with database.get_connection() as connection:
            plan_id = connection.execute("SELECT id FROM weekly_plans").fetchone()["id"]
        success, _ = complete_workout(
            "user-1",
            plan_id,
            [
                {"field_key": "distance", "value": "5.2"},
                {"field_key": "feeling", "value": "8"},
            ],
        )
        self.assertTrue(success)
        success, result = query_training_data(
            "user-1",
            {
                "scope": "group",
                "activity": "Run",
                "aggregation": "sum",
                "field_key": "distance",
            },
        )
        self.assertTrue(success)
        self.assertIn("5.2 km", result)

    def test_all_fields_are_required(self):
        create_activity(
            "user-1",
            "Swim",
            [
                {"display_name": "Distance", "field_type": "number", "unit": "m"},
                {"display_name": "Time", "field_type": "duration", "unit": "min"},
            ],
        )
        set_commitment("user-1", "Swim", 1)
        add_plan("user-1", "Swim", "utorok", "18:00")
        with database.get_connection() as connection:
            plan_id = connection.execute("SELECT id FROM weekly_plans").fetchone()["id"]
        success, message = complete_workout(
            "user-1", plan_id, [{"field_key": "distance", "value": "500"}]
        )
        self.assertFalse(success)
        self.assertIn("Time", message)

    def test_admin_approves_versioned_edit_and_deactivation(self):
        activity_service.ADMIN_DISCORD_USER_ID = "admin-1"
        create_activity(
            "user-1",
            "Yoga",
            [{"display_name": "Feeling", "field_type": "rating"}],
        )
        success, _ = request_activity_change(
            "user-1",
            "Yoga",
            "edit",
            "Mobility",
            [
                {"display_name": "Feeling", "field_type": "rating"},
                {"display_name": "Note", "field_type": "text"},
            ],
        )
        self.assertTrue(success)
        self.assertFalse(resolve_activity_change("user-1", 1, True)[0])
        self.assertTrue(resolve_activity_change("admin-1", 1, True)[0])
        self.assertEqual(2, list_activities()[0]["version_number"])
        self.assertTrue(request_activity_change("user-1", "Mobility", "deactivate")[0])
        self.assertTrue(resolve_activity_change("admin-1", 2, True)[0])
        self.assertEqual([], list_activities())
        self.assertFalse(set_commitment("user-1", "Mobility", 1)[0])

    def test_old_workout_keeps_original_activity_version(self):
        activity_service.ADMIN_DISCORD_USER_ID = "admin-1"
        create_activity(
            "user-1",
            "Run",
            [{"display_name": "Distance", "field_type": "number", "unit": "km"}],
        )
        set_commitment("user-1", "Run", 1)
        add_plan("user-1", "Run", "pondelok", "18:00")
        with database.get_connection() as connection:
            old_plan = connection.execute("SELECT * FROM weekly_plans").fetchone()
        complete_workout(
            "user-1", old_plan["id"], [{"field_key": "distance", "value": "5"}]
        )
        request_activity_change(
            "user-1",
            "Run",
            "edit",
            "Running",
            [
                {"display_name": "Distance", "field_type": "number", "unit": "km"},
                {"display_name": "Time", "field_type": "duration", "unit": "min"},
            ],
        )
        resolve_activity_change("admin-1", 1, True)
        with database.get_connection() as connection:
            current_version = connection.execute(
                "SELECT current_version_id FROM activity_types"
            ).fetchone()["current_version_id"]
            saved_plan = connection.execute(
                "SELECT activity_version_id FROM weekly_plans WHERE id = ?", (old_plan["id"],)
            ).fetchone()
        self.assertNotEqual(current_version, saved_plan["activity_version_id"])
        success, result = query_training_data(
            "user-1",
            {
                "scope": "self",
                "activity": "Running",
                "aggregation": "sum",
                "field_key": "distance",
            },
        )
        self.assertTrue(success)
        self.assertIn("5 km", result)

    def test_rules_are_read_from_fixed_document(self):
        self.assertIn("Pravidlá Couple GlowUp", get_rules())


class MigrationTest(unittest.TestCase):
    def test_migration_preserves_users_messages_and_legacy_training_data(self):
        with tempfile.TemporaryDirectory(dir=".") as temp:
            path = Path(temp) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY, discord_user_id TEXT UNIQUE,
                    display_name TEXT, created_at TEXT, is_active INTEGER DEFAULT 1
                );
                CREATE TABLE message_memory (
                    id INTEGER PRIMARY KEY, discord_user_id TEXT,
                    message_text TEXT, created_at TEXT
                );
                CREATE TABLE weekly_plans (id INTEGER PRIMARY KEY);
                INSERT INTO users VALUES (1, 'u', 'User', 'now', 1);
                INSERT INTO message_memory VALUES (1, 'u', 'hello', 'now');
                INSERT INTO weekly_plans VALUES (1);
                """
            )
            connection.commit()
            connection.close()
            original = database.DB_PATH
            database.DB_PATH = path
            try:
                database.init_database()
                database.init_database()
                with database.get_connection() as current:
                    self.assertEqual(3, current.execute("PRAGMA user_version").fetchone()[0])
                    self.assertEqual(1, current.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                    self.assertEqual(1, current.execute("SELECT COUNT(*) FROM message_memory").fetchone()[0])
                    self.assertEqual(0, current.execute("SELECT COUNT(*) FROM weekly_plans").fetchone()[0])
                    self.assertEqual(1, current.execute("SELECT COUNT(*) FROM weekly_plans_legacy_v1").fetchone()[0])
                    self.assertEqual(0, current.execute("SELECT COUNT(*) FROM activity_types").fetchone()[0])
            finally:
                database.DB_PATH = original


if __name__ == "__main__":
    unittest.main()
