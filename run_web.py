from __future__ import annotations

import uvicorn

from tg_directory_bot.config import load_config
from tg_directory_bot.time_utils import configure_beijing_timezone
from tg_directory_bot.web import create_app


def main() -> None:
    configure_beijing_timezone()
    config = load_config(require_bot_token=False)
    if len(config.admin_password) < 8:
        raise RuntimeError("ADMIN_PASSWORD must contain at least 8 characters")
    if len(config.session_secret) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
    uvicorn.run(create_app(config), host=config.web_host, port=config.web_port, proxy_headers=True)


if __name__ == "__main__":
    main()
