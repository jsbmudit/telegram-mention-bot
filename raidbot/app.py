"""Builds the bot: handlers, background jobs and startup checks."""

import logging
import time

from telegram import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, Update
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter, Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from .common import delete_later, get_db, is_group, safe_delete
from .config import Config
from .db import Database
from .h_backup import backup_cmd, backup_job
from .h_feed import feed_job, feedoff_cmd, feedon_cmd, feeds_cmd
from .h_lock import finish_lock, lock_cmd, unlock_cmd
from .h_members import (
    addusers_cmd, members_cmd, on_chat_member, on_left_member, on_my_chat_member, on_new_members,
    removeuser_cmd, track_message, welcome_cmd,
)
from .h_raid import (
    TRIGGER_RE, all_cmd, end_raid, endraid_button, endraid_cmd, on_trigger_text, raidall_cmd, tracking_job,
)
from .h_register import build_register_handler, me_cmd, register_in_group
from .h_reports import help_cmd, leaderboard_cmd, raidcsv_cmd, weekly_job
from .h_settings import build_settings_handler, deltemplate_cmd, template_cmd, templates_cmd
from .xapi import XClient

log = logging.getLogger(__name__)

GROUP_COMMANDS = [
    BotCommand("register", "Link your X account and Verona address"),
    BotCommand("leaderboard", "This week's leaderboard"),
    BotCommand("all", "Admins: tag everyone"),
    BotCommand("raidall", "Admins: tag everyone and post the raid"),
    BotCommand("endraid", "Admins: end a raid early"),
    BotCommand("raidlock", "Admins: lock chat, e.g. /raidlock 15 60m"),
    BotCommand("raidunlock", "Admins: unlock the chat"),
    BotCommand("addusers", "Admins: add @usernames to the tag list"),
    BotCommand("removeuser", "Admins: remove someone from the tag list"),
    BotCommand("members", "Admins: who is registered"),
    BotCommand("settemplate", "Admins: set a raid template, e.g. /settemplate fun"),
    BotCommand("templates", "Admins: list raid templates"),
    BotCommand("template", "Admins: preview a template, e.g. /template fun"),
    BotCommand("deltemplate", "Admins: delete a template"),
    BotCommand("setgreet", "Admins: set the welcome message"),
    BotCommand("greet", "Admins: post the welcome message"),
    BotCommand("raidcsv", "Admins: get a raid report privately"),
    BotCommand("feedon", "Admins: post an X account's posts here"),
    BotCommand("feedoff", "Admins: stop an X feed"),
    BotCommand("feeds", "Admins: list X feeds"),
    BotCommand("raidhelp", "All commands"),
]
PRIVATE_COMMANDS = [
    BotCommand("register", "Link your X account and Verona address"),
    BotCommand("me", "See what you registered"),
    BotCommand("cancel", "Stop registering"),
    BotCommand("raidhelp", "All commands"),
]


# Every command this bot answers in groups. These get removed from the chat
# a few seconds after they're sent, so the group stays clean.
OUR_GROUP_COMMANDS = {c.command for c in GROUP_COMMANDS} | {
    "allraid", "cancel", "me", "deltemplate", "templates", "backup",
}


async def cleanup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before everything else for group commands: schedule the command for deletion."""
    msg = update.effective_message
    if not is_group(update) or not msg or not msg.text or not msg.text.startswith("/"):
        return
    word = msg.text.split()[0][1:]
    name, _, target = word.partition("@")
    if target and target.lower() != (context.bot.username or "").lower():
        return  # a command for a different bot
    if name.lower() in OUR_GROUP_COMMANDS:
        delete_later(context, msg.chat_id, msg.message_id)


async def delete_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every few seconds: deletes messages whose time is up."""
    db = get_db(context)
    for row in db.due_deletes(time.time()):
        await safe_delete(context, row["chat_id"], row["message_id"])
        db.remove_scheduled(row["id"])


async def housekeeping_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every minute: raid and lock timers, cleanup."""
    db = get_db(context)
    now = time.time()
    for raid in db.active_raids():
        if raid["ends_at"] <= now:
            await end_raid(context, raid, "12 hours passed")
    for lock in db.active_locks():
        if lock["ends_at"] <= now:
            await finish_lock(context, lock, "time ran out")
    db.prune_links(now - 7 * 24 * 3600)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error while handling an update", exc_info=context.error)


async def post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
        await app.bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    except TelegramError as e:
        log.warning("Couldn't set the command menu: %s", e)
    me = await app.bot.get_me()
    log.info("Logged in as @%s", me.username)
    if app.bot_data.get("x") is None:
        log.warning("X_BEARER_TOKEN is not set: raids will run but comments won't be tracked.")
    if not app.bot_data["cfg"].owner_id:
        log.warning("BOT_OWNER_ID is not set: automatic backups are off.")


async def post_shutdown(app: Application) -> None:
    x = app.bot_data.get("x")
    if x:
        await x.close()
    db = app.bot_data.get("db")
    if db:
        db.close()


def build_app(cfg: Config, request=None, rate_limit: bool = True) -> Application:
    builder = Application.builder().token(cfg.telegram_token).post_init(post_init).post_shutdown(post_shutdown)
    if rate_limit:
        builder = builder.rate_limiter(AIORateLimiter(max_retries=3))
    if request is not None:  # used by tests to fake Telegram
        builder = builder.request(request).get_updates_request(request)
    app = builder.build()
    app.bot_data["cfg"] = cfg
    app.bot_data["db"] = Database(cfg.db_path)
    app.bot_data["x"] = XClient(cfg.x_bearer_token) if cfg.x_bearer_token else None

    # Only new messages: editing an old "@all" must not tag everyone again.
    new_msg = filters.UpdateType.MESSAGE
    groups = filters.ChatType.GROUPS & new_msg
    private = filters.ChatType.PRIVATE & new_msg

    # Group -2 runs first: tidy away our commands a few seconds after they're sent.
    app.add_handler(MessageHandler(groups & filters.COMMAND, cleanup_command), group=-2)
    # Group -1 sees every group message first, to keep the member list and links current.
    app.add_handler(MessageHandler(groups & ~filters.StatusUpdate.ALL, track_message), group=-1)

    app.add_handler(build_register_handler())
    app.add_handler(build_settings_handler())

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    commands = {
        "all": all_cmd, "raidall": raidall_cmd, "allraid": raidall_cmd, "endraid": endraid_cmd,
        "raidlock": lock_cmd, "raidunlock": unlock_cmd, "addusers": addusers_cmd, "removeuser": removeuser_cmd,
        "members": members_cmd, "template": template_cmd, "templates": templates_cmd,
        "deltemplate": deltemplate_cmd, "feedon": feedon_cmd, "feedoff": feedoff_cmd, "feeds": feeds_cmd, "greet": welcome_cmd, "raidcsv": raidcsv_cmd,
    }
    for name, fn in commands.items():
        app.add_handler(CommandHandler(name, fn, filters=groups))
    app.add_handler(CommandHandler("register", register_in_group, filters=groups))
    app.add_handler(CommandHandler("me", me_cmd, filters=private))
    app.add_handler(CommandHandler("backup", backup_cmd, filters=private | groups))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("raidhelp", help_cmd))
    app.add_handler(CommandHandler("help", help_cmd, filters=private))  # no clash with other bots in private
    app.add_handler(CallbackQueryHandler(endraid_button, pattern=r"^endraid:\d+$"))

    trigger = filters.Regex(TRIGGER_RE) | filters.CaptionRegex(TRIGGER_RE)
    app.add_handler(MessageHandler(groups & trigger & ~filters.COMMAND, on_trigger_text))

    app.add_error_handler(on_error)

    jq = app.job_queue
    jq.run_repeating(delete_job, interval=5, first=5, name="deletes")
    jq.run_repeating(housekeeping_job, interval=60, first=10, name="housekeeping")
    jq.run_repeating(tracking_job, interval=60, first=30, name="tracking")
    jq.run_repeating(feed_job, interval=60, first=45, name="feeds")
    jq.run_repeating(weekly_job, interval=3600, first=60, name="weekly")
    jq.run_repeating(backup_job, interval=600, first=120, name="backup")
    return app


def run(cfg: Config) -> None:
    app = build_app(cfg)
    app.run_polling(allowed_updates=Update.ALL_TYPES)
