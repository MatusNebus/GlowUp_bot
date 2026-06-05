from app.bot import run_bot
from app.database import init_database


def main() -> None:
    init_database()
    run_bot()


if __name__ == "__main__":
    main()
