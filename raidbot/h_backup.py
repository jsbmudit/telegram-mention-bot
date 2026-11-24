"""Backups of the bot's database, sent to the bot's owner in a private chat.

The database holds everything: registrations, members, templates, welcome
messages, feeds, raids and leaderboards. Keeping copies in the owner's
Telegram means they survive even if the server and its hosting account are lost.
Only the owner (BOT_OWNER_ID) gets them, because a backup holds every group's data.
"""

import logging
import time
from datetime import datetime, timezone

from telegram import InputFile, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .common import get_cfg, get_db, is_group

log = logging.getLogger(__name__)

LAST_BACKUP_KEY = "last_backup_at"
RETRY_AFTER = 3600  # if the owner can't be reached, try again an hour later
TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024  # bots can send files up to 50 MB


async def send_backup(context: ContextTypes.DEFAULT_TYPE, owner_id: int, reason: str) -> bool:
    """Send a fresh copy of the database to the owner. True if it arrived."""
    db = get_db(context)
    data = db.backup_bytes()
    now = datetime.now(timezone.utc)
    if len(data) > TELEGRAM_FILE_LIMIT:
        log.warning("The backup is %s MB, over Telegram's 50 MB limit for bots.", len(data) // (1024 * 1024))
        try:
            await context.bot.send_message(
                owner_id, "The backup is bigger than Telegram's 50 MB limit for bots, so I couldn't send it. "
                          "Copy the database file from the server instead.")
        except TelegramError:
            pass
        return False
    s = db.backup_stats()
    caption = (
        f"{reason}, {now:%Y-%m-%d %H:%M} UTC\n"
        f"Groups: {s['groups']}. Registered people: {s['registered']}. Raids: {s['raids']}. "
        f"Templates: {s['templates']}. Feeds: {s['feeds']}.\n\n"
        "To restore: stop the bot, save this file at the path DB_PATH points to, delete any "
        "-wal and -shm files next to it, then start the bot."
    )
    try:
        await context.bot.send_document(
            owner_id, InputFile(data, filename=f"raidbot_backup_{now:%Y-%m-%d_%H%M}UTC.db"), caption=caption,
        )
    except TelegramError as e:
        log.warning("Couldn't send the backup to the owner (they may need to press Start on the bot): %s", e)
        return False
    return True


async def backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every few minutes. Sends the automatic backup when it's due."""
    cfg = get_cfg(context)
    if not cfg.owner_id:
        return
    db = get_db(context)
    now = time.time()
    if now < context.bot_data.get("backup_retry_at", 0):
        return
    last = float(db.get_state(LAST_BACKUP_KEY) or 0)
    if now - last < cfg.backup_interval:
        return
    if await send_backup(context, cfg.owner_id, "Automatic backup"):
        db.set_state(LAST_BACKUP_KEY, str(now))
    else:
        context.bot_data["backup_retry_at"] = now + RETRY_AFTER


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backup: send a backup to the owner right now. Always privately, never in a group."""
    msg = update.effective_message
    owner_id = get_cfg(context).owner_id
    user = update.effective_user
    if is_group(update):
        # The command itself is removed from the group. Only the owner gets anything, privately.
        if owner_id and user and user.id == owner_id and not msg.sender_chat:
            if await send_backup(context, owner_id, "Backup you asked for"):
                get_db(context).set_state(LAST_BACKUP_KEY, str(time.time()))
        return
    if not owner_id:
        await msg.reply_text(
            "Backups go to the bot's owner, and no owner is set yet.\n"
            f"If that's you, your Telegram ID is {user.id}. Add BOT_OWNER_ID={user.id} to the bot's "
            "settings on the server and restart it."
        )
        return
    if user.id != owner_id:
        await msg.reply_text("Only the bot's owner can use /backup.")
        return
    if await send_backup(context, owner_id, "Backup you asked for"):
        get_db(context).set_state(LAST_BACKUP_KEY, str(time.time()))
    else:
        await msg.reply_text("Something went wrong making the backup. Try again in a minute.")
