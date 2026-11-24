"""Helpers shared by the Telegram handlers."""

import logging
import time

from telegram import ChatMember, Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .config import Config
from .db import Database
from .utils import extract_x_links
from .xapi import XClient

log = logging.getLogger(__name__)

ANONYMOUS_ADMIN_BOT_ID = 1087968824  # Telegram's "GroupAnonymousBot"
ADMIN_CACHE_SECONDS = 120
GROUP_TYPES = (ChatType.GROUP, ChatType.SUPERGROUP)


def get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.bot_data["db"]


def get_cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.bot_data["cfg"]


def get_x(context: ContextTypes.DEFAULT_TYPE) -> XClient | None:
    return context.bot_data.get("x")


def is_group(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type in GROUP_TYPES)


def sender_key(message: Message) -> int:
    """Who sent this: the user, or the group itself for anonymous admins."""
    if message.sender_chat:
        return message.sender_chat.id
    return message.from_user.id if message.from_user else 0


async def admin_ids(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> set[int]:
    cache = context.bot_data.setdefault("admin_cache", {})
    hit = cache.get(chat_id)
    if hit and time.time() - hit[0] < ADMIN_CACHE_SECONDS:
        return hit[1]
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
    except TelegramError as e:
        log.warning("Couldn't load admins for chat %s: %s", chat_id, e)
        return hit[1] if hit else set()
    ids = {a.user.id for a in admins if a.status in (ChatMember.ADMINISTRATOR, ChatMember.OWNER)}
    cache[chat_id] = (time.time(), ids)
    return ids


async def is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                   message: Message | None = None, user_id: int | None = None) -> bool:
    if message is not None:
        if message.sender_chat and message.sender_chat.id == chat_id:
            return True  # anonymous admin posting as the group
        if message.from_user:
            user_id = message.from_user.id
    if user_id is None:
        return False
    return user_id in await admin_ids(context, chat_id)


async def safe_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> bool:
    try:
        await context.bot.delete_message(chat_id, message_id)
        return True
    except TelegramError:
        return False


async def reply_temp(context: ContextTypes.DEFAULT_TYPE, message: Message, text: str,
                     seconds: int = 30, **kwargs) -> Message | None:
    """Reply in the group and have the reply delete itself later."""
    try:
        sent = await context.bot.send_message(
            message.chat_id, text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, **kwargs,
        )
    except TelegramError as e:
        log.warning("Couldn't send message in chat %s: %s", message.chat_id, e)
        return None
    get_db(context).schedule_delete(message.chat_id, sent.message_id, time.time() + seconds, "temp")
    return sent


def message_links(message: Message | None) -> list[tuple[str, str]]:
    """X post links in a message, including hidden links behind text."""
    if message is None:
        return []
    parts = [message.text or message.caption or ""]
    for ent in (message.entities or message.caption_entities or ()):
        if ent.url:
            parts.append(ent.url)
    return extract_x_links(" ".join(parts))


def bot_link(context: ContextTypes.DEFAULT_TYPE, payload: str = "") -> str:
    url = f"https://t.me/{context.bot.username}"
    return f"{url}?start={payload}" if payload else url
