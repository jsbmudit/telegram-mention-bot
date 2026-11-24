"""@all, @raidall, /endraid, polling X for replies, and ending raids."""

import asyncio
import html
import logging
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from . import core
from .common import (
    MEDIUM, delete_later, get_cfg, get_db, get_x, is_admin, is_group, message_links, reply_temp,
    send_temp, sender_key,
)
from .db import DEFAULT_TEMPLATE
from .utils import X_STATUS_RE, format_duration, handle_from_post_url, render_template, valid_template_name
from .xapi import XApiError, XRateLimited, oembed_author

log = logging.getLogger(__name__)

TRIGGER_RE = re.compile(r"(?<![\w@/])@(raidall|allraid|all)(?!\w)", re.IGNORECASE)
LINK_LOOKBACK = 24 * 3600


async def on_trigger_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A group message containing @all, @raidall or @allraid."""
    msg = update.effective_message
    text = msg.text or msg.caption or ""
    m = TRIGGER_RE.search(text)
    if not m:
        return
    raid = m.group(1).lower() != "all"
    before, after = text[:m.start()], text[m.end():]
    await start_tagging(update, context, raid, after, pure_check=before)


async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_tagging(update, context, False, " ".join(context.args or []))


async def raidall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_tagging(update, context, True, " ".join(context.args or []))


def find_raid_link(context: ContextTypes.DEFAULT_TYPE, msg: Message) -> tuple[str, str] | None:
    """Link in the same message, then in the message it replies to, then the
    latest link this admin posted in the last 24 hours."""
    for source in (msg, msg.reply_to_message):
        links = message_links(source)
        if links:
            return links[-1]
    row = get_db(context).latest_link(msg.chat_id, sender_key(msg), time.time() - LINK_LOOKBACK)
    return (row["tweet_id"], row["url"]) if row else None


def pick_template(db, chat_id: int, words: list[str]):
    """Choose the template for @raidall. Returns (template row, remaining words, error)."""
    templates = db.list_templates(chat_id)
    if not templates:
        return None, words, "There's no raid template yet. An admin can make one with /settemplate."
    first = words[0].lower() if words else ""
    if first:
        tpl = db.get_template(chat_id, first)
        if tpl:
            return tpl, words[1:], None
        if len(words) == 1 and valid_template_name(first):
            names = ", ".join(t["name"] for t in templates)
            return None, words, f"There's no template called {html.escape(first)}. Templates: {html.escape(names)}"
    tpl = db.get_template(chat_id, DEFAULT_TEMPLATE)
    if tpl:
        return tpl, words, None
    if len(templates) == 1:
        return templates[0], words, None
    names = ", ".join(t["name"] for t in templates)
    return None, words, f"Which template? Use @raidall name. Templates: {html.escape(names)}"


async def start_tagging(update: Update, context: ContextTypes.DEFAULT_TYPE, raid: bool,
                        args_text: str = "", pure_check: str | None = None) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    db, cfg = get_db(context), get_cfg(context)
    db.ensure_group(msg.chat_id, update.effective_chat.title)
    group = db.get_group(msg.chat_id)
    words = X_STATUS_RE.sub(" ", args_text).split()

    template = None
    if raid:
        template, words, error = pick_template(db, msg.chat_id, words)
        if error:
            await reply_temp(context, msg, error)
            return

    # "@all" or "@raidall fun <link>" on its own is just a command: tidy it away.
    # If the admin wrote a real message around it, leave their message alone.
    if pure_check is not None and not words and not X_STATUS_RE.sub(" ", pure_check).split():
        delete_later(context, msg.chat_id, msg.message_id)

    wait = group["last_tag_at"] + cfg.tag_cooldown - time.time()
    if wait > 0:
        await reply_temp(context, msg, f"Tagging is on cooldown. Try again in {format_duration(wait)}.")
        return

    link = None
    if raid:
        link = find_raid_link(context, msg)
        if not link:
            await reply_temp(
                context, msg,
                "I couldn't find an X post link. Post the link first, or put it in the same message as @raidall.",
            )
            return

    if not db.list_members(msg.chat_id):
        await reply_temp(context, msg, "I don't know any members yet. Add them with /addusers @name1 @name2.")
        if not raid:
            return

    db.set_group(msg.chat_id, "last_tag_at", time.time())
    # Tagging a big group takes a while because of Telegram's limits, so run it
    # in the background and keep answering other messages meanwhile.
    context.application.create_task(
        _run_tagging(context, msg.chat_id, sender_key(msg), link, template["name"] if template else None),
        update=update,
    )


async def _run_tagging(context: ContextTypes.DEFAULT_TYPE, chat_id: int, started_by: int,
                       link: tuple[str, str] | None, template_name: str | None) -> None:
    locks = context.bot_data.setdefault("tag_locks", {})
    lock = locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        db, cfg = get_db(context), get_cfg(context)
        delete_at = time.time() + cfg.tag_delete_after
        for text in core.build_tag_messages(db, chat_id, cfg.batch_size, {context.bot.id}):
            try:
                sent = await context.bot.send_message(
                    chat_id, text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )
                db.schedule_delete(chat_id, sent.message_id, delete_at, "tag")
            except TelegramError as e:
                log.warning("A tag batch failed in chat %s: %s", chat_id, e)
        if link:
            await _post_raid(context, chat_id, started_by, link[0], link[1], template_name)


async def lookup_author(context: ContextTypes.DEFAULT_TYPE, tweet_id: str, url: str):
    """Who posted the raid post: (conversation_id, display name, handle).

    Uses the X API when a key is set, otherwise X's public embed service.
    """
    conv, author = None, None
    x = get_x(context)
    if x:
        try:
            conv, author = await x.get_post_info(tweet_id)
        except XApiError as e:
            log.warning("Couldn't look up the raid post on X: %s", e)
    if author is None or not author.name:
        author = await oembed_author(tweet_id, handle_from_post_url(url)) or author
    name = author.name if author else None
    handle = (author.username if author and author.username else None) or handle_from_post_url(url)
    return conv, name, handle


async def _post_raid(context: ContextTypes.DEFAULT_TYPE, chat_id: int, started_by: int,
                     tweet_id: str, url: str, template_name: str) -> None:
    from .h_settings import post_template

    db, cfg = get_db(context), get_cfg(context)
    tpl = db.get_template(chat_id, template_name)
    if not tpl:
        await send_temp(context, chat_id, f"The template {html.escape(template_name)} was deleted. Raid not posted.", MEDIUM)
        return
    conv, xname, xhandle = await lookup_author(context, tweet_id, url)
    text = render_template(tpl["body_html"], url, xname, xhandle)
    try:
        main, _ = await post_template(context, chat_id, text, tpl["photo_file_id"])
    except TelegramError as e:
        log.warning("Couldn't post the raid message in chat %s: %s", chat_id, e)
        await send_temp(context, chat_id, "I couldn't post the raid message. Check the template with /template.", MEDIUM)
        return

    raid = db.create_raid(chat_id, tweet_id, url, started_by, cfg.raid_duration, main.message_id)
    if get_x(context):
        db.update_raid(raid["id"], conversation_id=conv or tweet_id)

    try:
        await context.bot.pin_chat_message(chat_id, main.message_id, disable_notification=False)
    except TelegramError as e:
        log.warning("Couldn't pin in chat %s: %s", chat_id, e)
        await send_temp(context, chat_id, "I couldn't pin the raid message. Give me the 'Pin messages' admin right.", MEDIUM)


# ---------------------------------------------------------------- tracking

async def poll_raid(context: ContextTypes.DEFAULT_TYPE, raid) -> None:
    """Fetch new replies for one raid and credit registered members."""
    x = get_x(context)
    if not x:
        return
    db = get_db(context)
    raid = db.get_raid(raid["id"])
    conv = raid["conversation_id"]
    if not conv:
        try:
            conv = await x.get_conversation_id(raid["tweet_id"]) or raid["tweet_id"]
        except XApiError:
            conv = raid["tweet_id"]
        db.update_raid(raid["id"], conversation_id=conv)
    replies, newest = await x.search_replies(conv, raid["since_id"])
    new = core.record_replies(db, raid, replies)
    fields = {"last_checked_at": time.time()}
    if newest:
        fields["since_id"] = newest
    db.update_raid(raid["id"], **fields)
    if new:
        log.info("Raid #%s in chat %s: %s new member comments", raid["raid_no"], raid["chat_id"], new)


async def tracking_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every minute. Checks each live raid when it's due."""
    x = get_x(context)
    if not x:
        return
    if time.time() < context.bot_data.get("x_paused_until", 0):
        return
    db, cfg = get_db(context), get_cfg(context)
    for raid in db.active_raids():
        lock = db.lock_for_raid(raid["id"])
        interval = cfg.lock_check_interval if lock else cfg.track_interval
        if time.time() - raid["last_checked_at"] < interval:
            continue
        try:
            await poll_raid(context, raid)
        except XRateLimited as e:
            context.bot_data["x_paused_until"] = e.retry_at
            log.warning("X rate limit hit. Pausing checks for %s.", format_duration(e.retry_at - time.time()))
            return
        except XApiError as e:
            log.warning("Checking raid #%s failed: %s", raid["raid_no"], e)
            db.update_raid(raid["id"], last_checked_at=time.time())
            continue
        if lock and db.engagement_count(raid["id"]) >= lock["target"]:
            from .h_lock import finish_lock
            await finish_lock(context, lock, "target reached")


# ---------------------------------------------------------------- ending

async def end_raid(context: ContextTypes.DEFAULT_TYPE, raid, reason: str) -> None:
    db = get_db(context)
    raid = db.get_raid(raid["id"])
    if raid["status"] != "active":
        return
    if get_x(context):
        try:
            await poll_raid(context, raid)  # one last check
        except XApiError as e:
            log.warning("Final check for raid #%s failed: %s", raid["raid_no"], e)
    if not db.finish_raid(raid["id"], reason):
        return
    lock = db.lock_for_raid(raid["id"])
    if lock:
        from .h_lock import finish_lock
        await finish_lock(context, lock, "the raid ended", post_summary=False)
    title = f"Raid #{raid['raid_no']} is over ({reason})"
    text = core.raid_summary_html(db, raid, title, tracking_on=bool(get_x(context)))
    try:
        await context.bot.send_message(
            raid["chat_id"], text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except TelegramError as e:
        log.warning("Couldn't post the raid summary in chat %s: %s", raid["chat_id"], e)


async def endraid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    raids = get_db(context).active_raids(msg.chat_id)
    if not raids:
        await reply_temp(context, msg, "No raid is running right now.")
        return
    if len(raids) == 1:
        await end_raid(context, raids[0], "ended by an admin")
        return
    buttons = [
        [InlineKeyboardButton(
            f"Raid #{r['raid_no']}, started {format_duration(time.time() - r['started_at'])} ago",
            callback_data=f"endraid:{r['id']}",
        )]
        for r in raids
    ]
    await reply_temp(context, msg, "More than one raid is running. Which one should end?", MEDIUM,
                     reply_markup=InlineKeyboardMarkup(buttons))


async def endraid_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = query.message.chat_id
    if not await is_admin(context, chat_id, user_id=query.from_user.id):
        await query.answer("Only admins can end a raid.", show_alert=True)
        return
    await query.answer()
    raid = get_db(context).get_raid(int(query.data.split(":")[1]))
    try:
        await query.message.delete()
    except TelegramError:
        pass
    if raid and raid["chat_id"] == chat_id:
        await end_raid(context, raid, "ended by an admin")
