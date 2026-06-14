import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "couple_glowup.db"
SCHEMA_VERSION = 2


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def get_connection() -> sqlite3.Connection:
    """Return a short-lived SQLite connection with dict-like rows."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_database() -> None:
    """Create the current schema and perform the one-time clean v2 migration."""
    with get_connection() as connection:
        _create_preserved_tables(connection)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            _migrate_to_dynamic_activities(connection)
        _create_dynamic_tables(connection)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _create_preserved_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS message_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL,
            author_display_name TEXT,
            channel_id TEXT,
            message_text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

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
        );
        """
    )
    _ensure_column(connection, "message_memory", "author_display_name", "TEXT")
    _ensure_column(connection, "message_memory", "channel_id", "TEXT")


def _migrate_to_dynamic_activities(connection: sqlite3.Connection) -> None:
    """Deliberately clear old training data before installing the dynamic model."""
    connection.execute("PRAGMA foreign_keys = OFF")
    for table in (
        "workout_log_values",
        "workout_logs",
        "jokers",
        "workout_replacement_votes",
        "workout_replacement_requests",
        "commitment_change_votes",
        "commitment_change_requests",
        "activity_change_requests",
        "weekly_plans",
        "commitments",
        "activity_fields",
        "activity_versions",
        "activity_types",
        "approved_activity_types",
        "onboarding_sessions",
        "pending_actions",
        "notification_log",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    connection.execute("PRAGMA foreign_keys = ON")


def _create_dynamic_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS activity_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            current_version_id INTEGER,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_by_discord_user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deactivated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS activity_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_type_id INTEGER NOT NULL,
            version_number INTEGER NOT NULL,
            display_name TEXT NOT NULL,
            created_by_discord_user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(activity_type_id, version_number),
            FOREIGN KEY(activity_type_id) REFERENCES activity_types(id)
        );

        CREATE TABLE IF NOT EXISTS activity_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            activity_version_id INTEGER NOT NULL,
            field_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            field_type TEXT NOT NULL CHECK(field_type IN ('number', 'duration', 'text', 'rating')),
            unit TEXT,
            position INTEGER NOT NULL,
            UNIQUE(activity_version_id, field_key),
            FOREIGN KEY(activity_version_id) REFERENCES activity_versions(id)
        );

        CREATE TABLE IF NOT EXISTS activity_change_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_discord_user_id TEXT NOT NULL,
            activity_type_id INTEGER NOT NULL,
            change_type TEXT NOT NULL CHECK(change_type IN ('edit', 'deactivate')),
            proposed_json TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by_discord_user_id TEXT,
            FOREIGN KEY(activity_type_id) REFERENCES activity_types(id)
        );

        CREATE TABLE IF NOT EXISTS commitments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            activity_type_id INTEGER NOT NULL,
            workout_type TEXT NOT NULL,
            count_per_week INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(activity_type_id) REFERENCES activity_types(id)
        );

        CREATE TABLE IF NOT EXISTS weekly_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            activity_version_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,
            workout_type TEXT NOT NULL,
            planned_day TEXT NOT NULL,
            planned_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'planned',
            joker_used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(activity_version_id) REFERENCES activity_versions(id)
        );

        CREATE TABLE IF NOT EXISTS workout_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            weekly_plan_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            activity_version_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(weekly_plan_id) REFERENCES weekly_plans(id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(activity_version_id) REFERENCES activity_versions(id)
        );

        CREATE TABLE IF NOT EXISTS workout_log_values (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workout_log_id INTEGER NOT NULL,
            activity_field_id INTEGER NOT NULL,
            value_text TEXT,
            value_number REAL,
            UNIQUE(workout_log_id, activity_field_id),
            FOREIGN KEY(workout_log_id) REFERENCES workout_logs(id),
            FOREIGN KEY(activity_field_id) REFERENCES activity_fields(id)
        );

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
        );

        CREATE TABLE IF NOT EXISTS notification_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_key TEXT UNIQUE NOT NULL,
            sent_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT NOT NULL,
            intent TEXT NOT NULL,
            original_message TEXT NOT NULL,
            missing_fields TEXT NOT NULL,
            parsed_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_resolved INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS onboarding_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id TEXT UNIQUE NOT NULL,
            last_answer TEXT,
            proposed_commitments TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

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
        );

        CREATE TABLE IF NOT EXISTS commitment_change_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            voter_discord_user_id TEXT NOT NULL,
            vote TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(request_id, voter_discord_user_id),
            FOREIGN KEY(request_id) REFERENCES commitment_change_requests(id)
        );

        CREATE TABLE IF NOT EXISTS workout_replacement_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_discord_user_id TEXT NOT NULL,
            original_weekly_plan_id INTEGER NOT NULL,
            replacement_activity_version_id INTEGER NOT NULL,
            replacement_workout_type TEXT NOT NULL,
            replacement_day TEXT NOT NULL,
            replacement_time TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            FOREIGN KEY(original_weekly_plan_id) REFERENCES weekly_plans(id),
            FOREIGN KEY(replacement_activity_version_id) REFERENCES activity_versions(id)
        );

        CREATE TABLE IF NOT EXISTS workout_replacement_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            voter_discord_user_id TEXT NOT NULL,
            vote TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(request_id, voter_discord_user_id),
            FOREIGN KEY(request_id) REFERENCES workout_replacement_requests(id)
        );
        """
    )


def _ensure_column(
    connection: sqlite3.Connection, table_name: str, column_name: str, definition: str
) -> None:
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    if any(column["name"] == column_name for column in columns):
        return
    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
