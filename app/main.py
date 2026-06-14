import logging

from app.bot import run_bot
from app.config import LOG_LEVEL
from app.database import init_database


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    init_database()
    run_bot()


if __name__ == "__main__":
    main()
