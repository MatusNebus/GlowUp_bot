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
                message_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
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
