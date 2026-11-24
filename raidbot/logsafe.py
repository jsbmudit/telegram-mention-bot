"""Logging setup that can never print the bot token or the X API key."""

import logging
import re

_TELEGRAM_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9%._~+/=-]+")


class RedactingFormatter(logging.Formatter):
    def __init__(self, secrets: list[str], fmt: str):
        super().__init__(fmt)
        self._secrets = [s for s in secrets if s]

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        text = _TELEGRAM_TOKEN_RE.sub("[REDACTED]", text)
        text = _BEARER_RE.sub(r"\1[REDACTED]", text)
        return text

    def format(self, record: logging.LogRecord) -> str:
        return self.redact(super().format(record))


def setup_logging(secrets: list[str]) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter(secrets, "%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # httpx logs every request URL at INFO, and Telegram URLs contain the token.
    for noisy in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
