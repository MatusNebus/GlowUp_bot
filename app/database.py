import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "couple_glowup.db"


def get_connection() -> sqlite3.Connection:
    """Vráti SQLite pripojenie a nastaví výstup riadkov ako dict-like objekty."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_database() -> None:
    """Vytvorí databázové tabuľky, ak ešte neexistujú."""
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS commitments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                workout_type TEXT NOT NULL,
                count_per_week INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                workout_type TEXT NOT NULL,
                planned_day TEXT NOT NULL,
                planned_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                joker_used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workout_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                weekly_plan_id INTEGER NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                workout_type TEXT NOT NULL,
                status TEXT NOT NULL,
                distance_km REAL,
                duration_minutes REAL,
                exercises_text TEXT,
                exercise_count INTEGER,
                set_count INTEGER,
                feeling INTEGER,
                note TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(weekly_plan_id) REFERENCES weekly_plans(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS jokers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                weekly_plan_id INTEGER NOT NULL,
                used_at TEXT NOT NULL,
                old_day TEXT NOT NULL,
                old_time TEXT NOT NULL,
                new_day TEXT NOT NULL,
                new_time TEXT NOT NULL,
                UNIQUE(user_id, week_start),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(weekly_plan_id) REFERENCES weekly_plans(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notification_key TEXT UNIQUE NOT NULL,
                sent_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS message_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT NOT NULL,
                author_display_name TEXT,
                channel_id TEXT,
                message_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        _ensure_column(connection, "message_memory", "author_display_name", "TEXT")
        _ensure_column(connection, "message_memory", "channel_id", "TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT NOT NULL,
                intent TEXT NOT NULL,
                original_message TEXT NOT NULL,
                missing_fields TEXT NOT NULL,
                parsed_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_resolved INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                goal TEXT,
                level TEXT,
                preferred_activities TEXT,
                limitations TEXT,
                weekly_capacity TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS onboarding_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT UNIQUE NOT NULL,
                last_answer TEXT,
                proposed_commitments TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS approved_activity_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workout_type TEXT UNIQUE NOT NULL,
                approved_by_discord_user_id TEXT NOT NULL,
                approved_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS commitment_change_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_discord_user_id TEXT NOT NULL,
                target_discord_user_id TEXT NOT NULL,
                workout_type TEXT NOT NULL,
                old_count INTEGER NOT NULL,
                new_count INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS commitment_change_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                voter_discord_user_id TEXT NOT NULL,
                vote TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(request_id, voter_discord_user_id),
                FOREIGN KEY(request_id) REFERENCES commitment_change_requests(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workout_replacement_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester_discord_user_id TEXT NOT NULL,
                original_weekly_plan_id INTEGER NOT NULL,
                replacement_workout_type TEXT NOT NULL,
                replacement_day TEXT NOT NULL,
                replacement_time TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY(original_weekly_plan_id) REFERENCES weekly_plans(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workout_replacement_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                voter_discord_user_id TEXT NOT NULL,
                vote TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(request_id, voter_discord_user_id),
                FOREIGN KEY(request_id) REFERENCES workout_replacement_requests(id)
            )
            """
        )


def _ensure_column(
    connection: sqlite3.Connection, table_name: str, column_name: str, definition: str
) -> None:
    """Doplní stĺpec do existujúcej SQLite tabuľky bez straty dát."""
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    if any(column["name"] == column_name for column in columns):
        return
    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
