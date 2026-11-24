"""Settings, read from environment variables only. Secrets never live in files."""

import os
from dataclasses import dataclass, field
from pathlib import Path


class MissingTokenError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    # repr=False keeps the secrets out of error messages and logs that print the config.
    telegram_token: str = field(repr=False)
    x_bearer_token: str | None = field(repr=False)
    db_path: str
    track_interval: int  # seconds between reply checks for a normal raid
    lock_check_interval: int  # seconds between reply checks while the chat is locked
    feed_interval: int = 60  # seconds between checks for new posts from followed X accounts
    raid_duration: int = 12 * 3600
    tag_cooldown: int = 15 * 60
    tag_delete_after: int = 4 * 3600
    welcome_delete_after: int = 24 * 3600
    batch_size: int = 5
    owner_id: int | None = None  # Telegram user ID of the bot's owner: gets backups and can run /backup
    backup_interval: int = 24 * 3600  # seconds between automatic backups


def load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a local .env file, for local testing only.

    Values already set in the real environment always win. The file is
    git-ignored and its contents are never printed.
    """
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_config() -> Config:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise MissingTokenError("TELEGRAM_BOT_TOKEN is not set")
    x_token = os.environ.get("X_BEARER_TOKEN", "").strip() or None
    return Config(
        telegram_token=token,
        x_bearer_token=x_token,
        db_path=os.environ.get("DB_PATH", "").strip() or "raidbot.db",
        track_interval=max(60, _int_env("TRACK_INTERVAL_MINUTES", 1) * 60),
        lock_check_interval=max(60, _int_env("LOCK_CHECK_INTERVAL_MINUTES", 1) * 60),
        feed_interval=max(60, _int_env("FEED_INTERVAL_MINUTES", 1) * 60),
        owner_id=_int_env("BOT_OWNER_ID", 0) or None,
        backup_interval=max(3600, _int_env("BACKUP_INTERVAL_HOURS", 24) * 3600),
    )


def secrets_to_redact() -> list[str]:
    """Every secret value the log redactor must hide."""
    return [
        v
        for v in (
            os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            os.environ.get("X_BEARER_TOKEN", "").strip(),
        )
        if v
    ]
