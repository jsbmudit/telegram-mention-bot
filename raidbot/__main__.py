"""Start the bot: python -m raidbot"""

import logging
import sys

from telegram.error import InvalidToken

from .config import MissingTokenError, load_config, load_dotenv, secrets_to_redact
from .logsafe import setup_logging


def main() -> int:
    load_dotenv()
    setup_logging(secrets_to_redact())
    log = logging.getLogger("raidbot")
    try:
        cfg = load_config()
    except MissingTokenError:
        print("TELEGRAM_BOT_TOKEN is not set. Add it as an environment variable and start the bot again.",
              file=sys.stderr)
        return 1

    from .app import run  # imported here so a missing token fails fast and clearly
    try:
        run(cfg)
    except InvalidToken:
        print("TELEGRAM_BOT_TOKEN was rejected by Telegram. Check that it's correct.", file=sys.stderr)
        return 1
    except Exception:
        log.exception("The bot stopped because of an error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
