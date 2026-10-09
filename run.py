from __future__ import annotations

import logging

from tg_directory_bot.bot import ALLOWED_UPDATES, build_application
from tg_directory_bot.config import load_config
from tg_directory_bot.time_utils import configure_beijing_timezone


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs, and Telegram embeds the bot token in that URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    configure_beijing_timezone()
    configure_logging()
    config = load_config()
    app = build_application(config)
    app.run_polling(
        allowed_updates=ALLOWED_UPDATES
    )


if __name__ == "__main__":
    main()
