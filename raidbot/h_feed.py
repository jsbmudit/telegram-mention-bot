"""Forward an X account's posts into the group. /feedon, /feedoff, /feeds.

Needs the X API key: X has no free, reliable way to read an account's posts.
"""

import html
import logging
import re
import time
from collections import defaultdict

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .common import MEDIUM, SHORT, get_cfg, get_db, get_x, is_admin, is_group, reply_temp
from .utils import format_duration, is_due, normalize_x_handle
from .xapi import Post, XApiError, XRateLimited

log = logging.getLogger(__name__)

MAX_POSTS_PER_CHECK = 10   # if the bot was offline for a while, don't flood the group
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
_TRAILING_TCO = re.compile(r"\s*https://t\.co/\w+\s*$")
FEEDON_USAGE = (
    "Usage: /feedon VeronaOfficial\n"
    "Add 'all' to include replies and reposts: /feedon VeronaOfficial all\n"
    "Add 'pin' to pin each new post: /feedon VeronaOfficial pin\n"
    "Both work together: /feedon VeronaOfficial all pin"
)
NO_KEY = "The X feed needs the X API key (X_BEARER_TOKEN). Ask whoever runs the bot to add it."


# ---------------------------------------------------------------- commands

async def feedon_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    args = context.args or []
    handle = normalize_x_handle(args[0]) if args else None
    options = {a.lower() for a in args[1:]}
    if not handle or not options <= {"all", "pin"} or len(args) - 1 != len(options):
        await reply_temp(context, msg, FEEDON_USAGE)
        return
    mode = "all" if "all" in options else "posts"
    pin = "pin" in options
    x = get_x(context)
    if not x:
        await reply_temp(context, msg, NO_KEY)
        return
    try:
        user = await x.lookup_user(handle)
        if not user:
            await reply_temp(context, msg, f"I couldn't find @{html.escape(handle)} on X.")
            return
        # Start from their newest post so old posts aren't dumped into the group.
        latest = await x.user_posts(user["id"], include_all=True, max_results=5, max_pages=1)
    except XApiError as e:
        log.warning("Feed setup failed: %s", e)
        await reply_temp(context, msg, "X didn't answer properly. Try again in a few minutes.")
        return
    since = max((p.id for p in latest), key=int, default=None)
    get_db(context).add_feed(msg.chat_id, user["id"], user["username"], user["name"], mode, since, pin)
    what = "posts, replies and reposts" if mode == "all" else "posts"
    interval = get_cfg(context).feed_interval
    every = "minute" if interval == 60 else format_duration(interval)
    await reply_temp(
        context, msg,
        f"X feed on: new {what} from <b>{html.escape(user['name'] or user['username'])}</b> "
        f"(@{html.escape(user['username'])}) will be posted here"
        f"{' and pinned with a notification' if pin else ''}. I check every {every}.\n"
        f"Turn it off with /feedoff {html.escape(user['username'])}",
        SHORT,  # only the admin needs it, so it goes quickly like other confirmations
    )


async def feedoff_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    handle = normalize_x_handle(context.args[0]) if context.args else None
    if not handle:
        await reply_temp(context, msg, "Usage: /feedoff VeronaOfficial (see what's on with /feeds)")
        return
    if get_db(context).remove_feed(msg.chat_id, handle):
        await reply_temp(context, msg, f"X feed off for @{html.escape(handle)}.")
    else:
        await reply_temp(context, msg, f"There's no feed for @{html.escape(handle)} here. See /feeds.")


async def feeds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    feeds = get_db(context).feeds_for_chat(msg.chat_id)
    if not feeds:
        await reply_temp(context, msg, "No X feeds are on. Start one with /feedon VeronaOfficial")
        return
    lines = ["<b>X feeds in this group</b>"]
    for f in feeds:
        what = "everything" if f["mode"] == "all" else "posts"
        if f["pin"]:
            what += ", pinned"
        lines.append(f"@{html.escape(f['handle'])} ({what}): turn off with /feedoff {html.escape(f['handle'])}")
    if not get_x(context):
        lines += ["", NO_KEY]
    await reply_temp(context, msg, "\n".join(lines), MEDIUM)


# ---------------------------------------------------------------- posting

def format_post(name: str | None, handle: str, post: Post) -> tuple[str, str]:
    """(full message HTML, short caption HTML) for one X post."""
    label = {"reply": "replied on X", "repost": "reposted on X", "quote": "quoted a post on X"}.get(post.kind, "posted on X")
    header = f"<b>{html.escape(name or handle)}</b> (@{html.escape(handle)}) {label}"
    text = _TRAILING_TCO.sub("", post.text) if post.photo_url else post.text
    link = f"https://x.com/{handle}/status/{post.id}"

    def build(limit: int) -> str:
        room = limit - len(header) - len(link) - 10
        body = text if len(text) <= room else text[: max(0, room - 3)].rstrip() + "..."
        return f"{header}\n\n{html.escape(body)}\n\n{link}" if body else f"{header}\n\n{link}"

    return build(MESSAGE_LIMIT), build(CAPTION_LIMIT)


async def send_post(context: ContextTypes.DEFAULT_TYPE, chat_id: int, name: str | None,
                    handle: str, post: Post) -> Message:
    full, caption = format_post(name, handle, post)
    if post.photo_url:
        try:
            return await context.bot.send_photo(chat_id, post.photo_url, caption=caption, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            log.info("Couldn't send the post's image, sending text instead: %s", e)
    return await context.bot.send_message(chat_id, full, parse_mode=ParseMode.HTML)


async def feed_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every minute. Checks each followed X account when it's due."""
    x = get_x(context)
    if not x or time.time() < context.bot_data.get("x_paused_until", 0):
        return
    db, cfg = get_db(context), get_cfg(context)
    by_account = defaultdict(list)
    for f in db.all_feeds():
        by_account[f["x_user_id"]].append(f)

    for x_user_id, feeds in by_account.items():
        now = time.time()
        if not is_due(min(f["last_checked_at"] for f in feeds), cfg.feed_interval, now):
            continue
        # One request per account, even if several groups follow it.
        known = [f["since_id"] for f in feeds]
        since = None if None in known else min(known, key=int)
        include_all = any(f["mode"] == "all" for f in feeds)
        try:
            posts = await x.user_posts(x_user_id, since_id=since, include_all=include_all)
        except XRateLimited as e:
            context.bot_data["x_paused_until"] = e.retry_at
            log.warning("X rate limit hit. Pausing X checks for %s.", format_duration(e.retry_at - time.time()))
            return
        except XApiError as e:
            log.warning("Checking X feed for @%s failed: %s", feeds[0]["handle"], e)
            for f in feeds:
                db.update_feed(f["id"], f["since_id"], now)
            continue

        posts.sort(key=lambda p: int(p.id))  # oldest first, so they read in order
        newest = posts[-1].id if posts else None
        for f in feeds:
            fresh = [p for p in posts if f["since_id"] is None or int(p.id) > int(f["since_id"])]
            if f["mode"] != "all":
                fresh = [p for p in fresh if p.kind in ("post", "quote")]
            for p in fresh[-MAX_POSTS_PER_CHECK:]:
                try:
                    sent = await send_post(context, f["chat_id"], f["name"], f["handle"], p)
                except TelegramError as e:
                    log.warning("Couldn't post X feed item in chat %s: %s", f["chat_id"], e)
                    continue
                if f["pin"]:
                    try:  # pinned with a notification, like a raid post
                        await context.bot.pin_chat_message(f["chat_id"], sent.message_id, disable_notification=False)
                    except TelegramError as e:
                        log.warning("Couldn't pin X feed item in chat %s: %s", f["chat_id"], e)
            new_since = f["since_id"]
            if newest and (new_since is None or int(newest) > int(new_since)):
                new_since = newest
            db.update_feed(f["id"], new_since, now)
