"""/raidcsv, /leaderboard, /help and the weekly leaderboard post."""

import logging
import time
from datetime import timedelta

from telegram import InputFile, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from . import core
from .common import (
    MEDIUM, ANONYMOUS_ADMIN_BOT_ID, admin_ids, bot_link, get_db, is_admin, is_group, reply_temp, safe_delete,
)
from .utils import week_start

log = logging.getLogger(__name__)


async def raidcsv_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    await safe_delete(context, msg.chat_id, msg.message_id)
    db = get_db(context)
    arg = (context.args or [""])[0].lower()

    if arg == "all":
        raids = db.list_raids(msg.chat_id)
        label = "all"
    elif arg.isdigit():
        raid = db.get_raid_by_no(msg.chat_id, int(arg))
        raids = [raid] if raid else []
        label = f"raid{arg}"
    elif arg == "":
        raid = db.latest_raid(msg.chat_id)
        raids = [raid] if raid else []
        label = f"raid{raid['raid_no']}" if raid else ""
    else:
        await reply_temp(context, msg, "Usage: /raidcsv, /raidcsv 3 or /raidcsv all")
        return
    if not raids:
        await reply_temp(context, msg, "I couldn't find that raid.")
        return

    user = msg.from_user
    if msg.sender_chat or not user or user.id == ANONYMOUS_ADMIN_BOT_ID:
        await reply_temp(
            context, msg,
            "I can't send files to anonymous admins. Turn off 'Remain anonymous' and try again.", 30,
        )
        return

    title = (update.effective_chat.title or "group").replace(" ", "_")[:40]
    data = core.raid_csv(db, msg.chat_id, raids)
    try:
        await context.bot.send_document(
            user.id, InputFile(data, filename=f"{title}_{label}.csv"),
            caption=f"Raid report for {update.effective_chat.title or 'your group'}",
        )
    except Forbidden:
        await reply_temp(
            context, msg,
            f'{user.mention_html()}, I can\'t message you yet. Open <a href="{bot_link(context)}">a chat with me</a>, '
            "press Start, then run /raidcsv again.",
        )
    except TelegramError as e:
        log.warning("Couldn't send CSV: %s", e)
        await reply_temp(context, msg, "Something went wrong sending the file. Try again.")


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update):
        await msg.reply_text("Use /leaderboard inside your group.")
        return
    start = week_start(time.time())
    rows = core.leaderboard(get_db(context), msg.chat_id, start.timestamp(),
                            (start + timedelta(days=7)).timestamp())
    await reply_temp(context, msg, core.leaderboard_html(rows, start.timestamp(), "Leaderboard this week"), MEDIUM)


async def weekly_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every hour. Posts last week's leaderboard once, on Monday (UTC)."""
    db = get_db(context)
    this_week = week_start(time.time())
    last_week = this_week - timedelta(days=7)
    key = last_week.date().isoformat()
    for group in db.all_groups():
        if group["last_lb_week"] == key:
            continue
        db.set_group(group["chat_id"], "last_lb_week", key)
        rows = core.leaderboard(db, group["chat_id"], last_week.timestamp(), this_week.timestamp())
        if not rows:
            continue
        chat_id = group["chat_id"]
        try:
            await context.bot.send_message(
                chat_id, core.leaderboard_html(rows, last_week.timestamp(), "Weekly leaderboard"),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            log.warning("Couldn't post leaderboard in chat %s: %s", chat_id, e)
            continue
        data = core.top_csv(rows, 5)
        name = (group["title"] or "group").replace(" ", "_")[:40]
        for admin_id in await admin_ids(context, chat_id):
            if admin_id == context.bot.id:
                continue
            try:
                await context.bot.send_document(
                    admin_id, InputFile(data, filename=f"{name}_top5_{key}.csv"),
                    caption=f"Top 5 for the week of {key} in {group['title'] or 'your group'}, with Verona addresses.",
                )
            except TelegramError:
                pass  # this admin hasn't pressed Start on the bot


HELP_TEXT = """<b>Raid bot commands</b>

<b>Everyone</b>
/register: link your X account and Verona address (in a private chat with me)
/me: see what you registered (private chat)
/leaderboard: this week's leaderboard

<b>Admins: tagging and raids</b>
@all: tag everyone (15 min cooldown, tags delete after 4h)
@raidall: tag everyone, then post and pin the default raid message
@raidall fun: same, using the template called fun
/endraid: end a raid early
/lock 15 60m: lock the chat until 15 members comment, or for 60 min
/unlock: unlock the chat

<b>Admins: templates</b>
/settemplate: set the default raid template
/settemplate fun: set a template called fun (send a photo with caption to attach an image)
/templates: list all templates
/template fun: preview a template
/deltemplate fun: delete a template
Variables: {link} {xname} {xhandle} {xprofile}

<b>Admins: setup</b>
/addusers @a @b: add people to the tag list
/removeuser @a: remove someone from the tag list
/members: who is and isn't registered (sent privately)
/setwelcome: set the welcome message (use {name})
/welcome: post the welcome message

<b>Admins: reports</b> (sent to you privately)
/raidcsv, /raidcsv 3, /raidcsv all"""


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if is_group(update):
        await reply_temp(context, msg, HELP_TEXT, MEDIUM)
    else:
        await msg.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)
