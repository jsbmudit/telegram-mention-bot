"""Keeping the member list up to date, /addusers, /removeuser, /members, and welcome messages."""

import html
import logging
import re
import time

from telegram import ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .common import (
    MEDIUM, bot_link, get_cfg, get_db, is_admin, is_group, message_links, safe_delete, tell_admin,
    sender_key,
)
from .utils import mention_html

log = logging.getLogger(__name__)

DEFAULT_WELCOME = (
    "Welcome {name}!\n\nRegister your X account and Verona address with the button below "
    "so your raids get tracked."
)
_USERNAME_RE = re.compile(r"@([A-Za-z0-9_]{4,32})")
_VALID_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{4,32}")


def _remember(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: User) -> None:
    if user.is_bot:
        return
    db = get_db(context)
    db.upsert_user_profile(user.id, user.username, user.first_name)
    db.upsert_member(chat_id, user.id, user.username, user.full_name)
    db.link_username_everywhere(user.id, user.username, user.full_name)


async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs on every group message: remember the sender and any X links they post."""
    if not is_group(update) or not update.effective_message:
        return
    msg = update.effective_message
    db = get_db(context)
    db.ensure_group(msg.chat_id, update.effective_chat.title)
    if msg.from_user and not msg.sender_chat:
        _remember(context, msg.chat_id, msg.from_user)
    for tweet_id, url in message_links(msg):
        db.add_link(msg.chat_id, sender_key(msg), tweet_id, url)


# ---------------------------------------------------------------- joins and leaves

def _recently_welcomed(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    seen = context.bot_data.setdefault("welcomed", {})
    now = time.time()
    for key in [k for k, t in seen.items() if now - t > 300]:
        del seen[key]
    if (chat_id, user_id) in seen:
        return True
    seen[(chat_id, user_id)] = now
    return False


async def _on_join(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: User) -> None:
    if user.is_bot:
        return
    _remember(context, chat_id, user)
    if _recently_welcomed(context, chat_id, user.id):
        return
    if get_db(context).get_user(user.id)["registered_at"]:
        return  # already registered from another group
    await send_welcome(context, chat_id, user)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    get_db(context).ensure_group(msg.chat_id, update.effective_chat.title)
    for user in msg.new_chat_members or []:
        if user.id == context.bot.id:
            await _introduce(context, msg.chat_id)
        else:
            await _on_join(context, msg.chat_id, user)


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = msg.left_chat_member
    if user and not user.is_bot:
        get_db(context).remove_member(msg.chat_id, user.id)


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Join and leave updates. Needs the bot to be an admin."""
    cmu = update.chat_member
    if not cmu:
        return
    member_states = (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER)
    was_in = cmu.old_chat_member.status in member_states or (
        cmu.old_chat_member.status == ChatMember.RESTRICTED and cmu.old_chat_member.is_member)
    is_in = cmu.new_chat_member.status in member_states or (
        cmu.new_chat_member.status == ChatMember.RESTRICTED and cmu.new_chat_member.is_member)
    user = cmu.new_chat_member.user
    get_db(context).ensure_group(cmu.chat.id, cmu.chat.title)
    if is_in and not was_in:
        await _on_join(context, cmu.chat.id, user)
    elif was_in and not is_in and not user.is_bot:
        get_db(context).remove_member(cmu.chat.id, user.id)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The bot itself was added to a group or promoted."""
    cmu = update.my_chat_member
    if not cmu or not is_group(update):
        return
    get_db(context).ensure_group(cmu.chat.id, cmu.chat.title)
    old, new = cmu.old_chat_member.status, cmu.new_chat_member.status
    if old in (ChatMember.LEFT, ChatMember.BANNED) and new in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR):
        await _introduce(context, cmu.chat.id)


async def _introduce(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    if _recently_welcomed(context, chat_id, context.bot.id):
        return
    text = (
        "Hi! I'm the raid bot.\n\n"
        "To work properly I need to be an <b>admin</b> with these rights: delete messages, "
        "pin messages and ban users (needed for /raidlock).\n\n"
        "Admins: add your members with /addusers @name1 @name2, set a raid template with "
        "/settemplate, then use @all or @raidall. Send /raidhelp for all commands."
    )
    try:
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        log.warning("Couldn't introduce myself in chat %s: %s", chat_id, e)


# ---------------------------------------------------------------- welcome

async def send_welcome(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user: User | None) -> None:
    group = get_db(context).get_group(chat_id)
    template = (group["welcome_html"] if group else None) or DEFAULT_WELCOME
    name = mention_html(user.id, user.username, user.full_name) if user else "everyone"
    text = template.replace("{name}", name)
    button = InlineKeyboardMarkup([[InlineKeyboardButton("Register", url=bot_link(context, "register"))]])
    try:
        sent = await context.bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, reply_markup=button, disable_web_page_preview=True
        )
    except TelegramError as e:
        log.warning("Couldn't send welcome in chat %s: %s", chat_id, e)
        return
    get_db(context).schedule_delete(
        chat_id, sent.message_id, time.time() + get_cfg(context).welcome_delete_after,
        "welcome", user.id if user else None,
    )


async def welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    target = msg.reply_to_message.from_user if msg.reply_to_message else None
    if target and target.is_bot:
        target = None
    await safe_delete(context, msg.chat_id, msg.message_id)
    await send_welcome(context, msg.chat_id, target)


# ---------------------------------------------------------------- admin member commands

async def addusers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    db = get_db(context)
    db.ensure_group(msg.chat_id, update.effective_chat.title)
    names, skipped = [], []
    for name in re.findall(r"@(\w+)", msg.text or ""):
        if not _VALID_USERNAME_RE.fullmatch(name):
            skipped.append("@" + name)
        elif name.lower() not in {n.lower() for n in names}:
            names.append(name)
    linked = 0
    for ent in msg.entities or ():
        if ent.type == "text_mention" and ent.user and not ent.user.is_bot:
            _remember(context, msg.chat_id, ent.user)
            linked += 1
    if not names and not linked:
        await tell_admin(context, msg, "Usage: /addusers @name1 @name2 @name3")
        return
    added, existing = db.add_usernames(msg.chat_id, names)
    total = len(db.list_members(msg.chat_id))
    text = f"Added {added + linked}. Already on the list: {existing}. Members I know now: {total}."
    if skipped:
        text += "\nSkipped (not valid Telegram usernames): " + ", ".join(skipped)
    await tell_admin(context, msg, text)


async def removeuser_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    names = _USERNAME_RE.findall(msg.text or "")
    if not names:
        await tell_admin(context, msg, "Usage: /removeuser @name")
        return
    db = get_db(context)
    removed = sum(db.remove_member_by_username(msg.chat_id, n) for n in names)
    await tell_admin(context, msg, f"Removed {removed} from the tag list.")


async def members_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    members = get_db(context).list_members(msg.chat_id)
    reg = [m for m in members if m["registered"]]
    unreg = [m for m in members if not m["registered"]]
    lines = [
        f"<b>Members I know: {len(members)}</b>",
        f"Registered: {len(reg)}",
        f"Not registered: {len(unreg)}",
    ]
    if unreg:
        names = [html.escape(m["username"] or m["name"] or "member") for m in unreg]
        lines += ["", "<b>Not registered</b>", ", ".join(names[:100])]
        if len(names) > 100:
            lines.append(f"and {len(names) - 100} more")
    await tell_admin(context, msg, "\n".join(lines), MEDIUM)
