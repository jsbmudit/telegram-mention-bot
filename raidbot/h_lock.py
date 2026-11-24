"""/raidlock and /raidunlock."""

import json
import logging

from telegram import ChatPermissions, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from . import core
from .common import get_db, get_x, is_admin, is_group, reply_temp, safe_delete
from .utils import format_duration, parse_duration

log = logging.getLogger(__name__)

USAGE = "Usage: /raidlock 15 60m\nThat locks the chat until 15 members comment on the raid, or for 60 minutes."

# Used only if Telegram didn't tell us the group's permissions before locking.
FALLBACK_PERMISSIONS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
    can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True,
    can_send_other_messages=True, can_add_web_page_previews=True, can_invite_users=True,
)


async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    db = get_db(context)
    args = context.args or []
    if len(args) != 2 or not args[0].isdigit() or int(args[0]) < 1:
        await reply_temp(context, msg, USAGE)
        return
    target = int(args[0])
    duration = parse_duration(args[1])
    if not duration:
        await reply_temp(context, msg, "I didn't understand the time. Use something like 60m or 2h (max 24h).")
        return
    if db.active_lock(msg.chat_id):
        await reply_temp(context, msg, "The chat is already locked. Use /raidunlock first.")
        return
    raids = db.active_raids(msg.chat_id)
    if not raids:
        await reply_temp(context, msg, "There's no live raid. Start one with @raidall first.")
        return
    raid = raids[-1]  # the most recent live raid
    have = db.engagement_count(raid["id"])
    if have >= target:
        await reply_temp(context, msg, f"Raid #{raid['raid_no']} already has {have} comments, so there's nothing to lock for.")
        return

    try:
        chat = await context.bot.get_chat(msg.chat_id)
        prev = json.dumps(chat.permissions.to_dict()) if chat.permissions else None
        await context.bot.set_chat_permissions(
            msg.chat_id, ChatPermissions.no_permissions(), use_independent_chat_permissions=True
        )
    except TelegramError as e:
        log.warning("Couldn't lock chat %s: %s", msg.chat_id, e)
        await reply_temp(context, msg, "I couldn't lock the chat. Give me the 'Ban users' admin right and try again.")
        return

    lock = db.create_lock(msg.chat_id, raid["id"], target, duration, prev)
    text = (
        f"<b>Chat locked</b> for raid #{raid['raid_no']}.\n"
        f"It opens when {target} registered members have commented ({have} so far), "
        f"or in {format_duration(duration)}, whichever comes first.\n\n"
        f"Go comment: {raid['url']}"
    )
    if not get_x(context):
        text += "\n\nComment tracking is off (no X API key), so it will only open on the timer or with /raidunlock."
    try:
        notice = await context.bot.send_message(
            msg.chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
        db.set_lock_message(lock["id"], notice.message_id)  # removed again when the chat unlocks
    except TelegramError as e:
        log.warning("Couldn't post lock notice in chat %s: %s", msg.chat_id, e)


async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    lock = get_db(context).active_lock(msg.chat_id)
    if not lock:
        await reply_temp(context, msg, "The chat isn't locked.")
        return
    await finish_lock(context, lock, "unlocked by an admin")


async def finish_lock(context: ContextTypes.DEFAULT_TYPE, lock, reason: str, post_summary: bool = True) -> None:
    db = get_db(context)
    chat_id = lock["chat_id"]
    perms = FALLBACK_PERMISSIONS
    if lock["prev_permissions"]:
        perms = ChatPermissions.de_json(json.loads(lock["prev_permissions"]), context.bot)
    try:
        await context.bot.set_chat_permissions(chat_id, perms, use_independent_chat_permissions=True)
    except TelegramError as e:
        log.warning("Couldn't unlock chat %s: %s", chat_id, e)
        try:
            await context.bot.send_message(chat_id, "I couldn't unlock the chat. An admin needs to restore permissions by hand.")
        except TelegramError:
            pass
        return  # keep the lock active so the next check retries
    if not db.finish_lock(lock["id"], reason):
        return
    if lock["message_id"]:
        await safe_delete(context, chat_id, lock["message_id"])
    raid = db.get_raid(lock["raid_id"])
    if post_summary and raid:
        title = f"Chat unlocked: {reason}"
        text = core.raid_summary_html(db, raid, title, tracking_on=bool(get_x(context)))
    else:
        text = f"<b>Chat unlocked:</b> {reason}."
    try:
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except TelegramError as e:
        log.warning("Couldn't post unlock message in chat %s: %s", chat_id, e)
