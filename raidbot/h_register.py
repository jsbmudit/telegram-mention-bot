"""/register: a short private chat that asks for the X account and Verona address."""

import html
import logging
import re
import warnings

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters,
)

from .common import bot_link, delete_later, get_db, get_x, safe_delete
from .utils import normalize_x_handle, validate_xion_address
from .xapi import XApiError
from telegram.warnings import PTBUserWarning

# The confirm button is only ever pressed on the bot's own last message, so
# per-message tracking isn't needed. Silence the library's generic warning.
warnings.filterwarnings("ignore", message=r"If 'per_message=False'", category=PTBUserWarning)

log = logging.getLogger(__name__)

ASK_X, ASK_WALLET, CONFIRM = range(3)


REGISTER_PROMPT_SECONDS = 15
_PROMPT_PAYLOAD_RE = re.compile(r"^reg(-?\d+)_(\d+)$")


async def register_in_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/register typed in a group: point the person to a private chat with the bot.

    The button's link carries this message's ID, so the moment someone opens
    the private chat through it, the prompt is deleted from the group.
    Otherwise it deletes itself after 15 seconds.
    """
    msg = update.effective_message
    try:
        sent = await context.bot.send_message(
            msg.chat_id, "Tap the button to register in a private chat with me. It takes 30 seconds.",
        )
        link = bot_link(context, f"reg{msg.chat_id}_{sent.message_id}")
        await sent.edit_reply_markup(InlineKeyboardMarkup([[InlineKeyboardButton("Register", url=link)]]))
    except TelegramError as e:
        log.warning("Couldn't post the register button in chat %s: %s", msg.chat_id, e)
        return
    delete_later(context, msg.chat_id, sent.message_id, REGISTER_PROMPT_SECONDS, kind="register_prompt")


async def start_private(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    get_db(context).upsert_user_profile(user.id, user.username, user.first_name)
    payload = context.args[0] if context.args else ""
    m = _PROMPT_PAYLOAD_RE.match(payload)
    if m:
        # Only delete it if it really is one of our register prompts. The link
        # text can be edited by anyone, so never trust it on its own.
        chat_id, message_id = int(m.group(1)), int(m.group(2))
        row = get_db(context).find_scheduled(chat_id, message_id, "register_prompt")
        if row:
            await safe_delete(context, chat_id, message_id)
            get_db(context).remove_scheduled(row["id"])
        return await begin(update, context)
    if payload == "register":
        return await begin(update, context)
    await update.effective_message.reply_text(
        "Hi! I run raids in your Telegram group.\n\n"
        "Send /register to link your X account and Verona address so your raids get tracked.\n"
        "Send /me to see what you registered.\n"
        "Send /help for all commands."
    )
    return ConversationHandler.END


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    get_db(context).upsert_user_profile(user.id, user.username, user.first_name)
    context.user_data.pop("reg", None)
    await update.effective_message.reply_text(
        "Let's get you registered.\n\n"
        "<b>Step 1 of 2:</b> send your X username or profile link.\n"
        "For example: @yourname or https://x.com/yourname\n\n"
        "Send /cancel to stop.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    return ASK_X


async def got_x(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    handle = normalize_x_handle(msg.text or "")
    if not handle:
        await msg.reply_text(
            "That doesn't look like an X username. Send it like @yourname or https://x.com/yourname"
        )
        return ASK_X

    x_user_id = None
    x = get_x(context)
    if x:
        try:
            found = await x.get_user(handle)
        except XApiError as e:
            log.warning("X lookup failed during registration: %s", e)
            found = ("", handle)  # can't check right now, accept as typed
        if found is None:
            await msg.reply_text(f"I couldn't find @{handle} on X. Check the spelling and send it again.")
            return ASK_X
        x_user_id, handle = (found[0] or None), found[1]

    owner = get_db(context).x_owner(handle, x_user_id)
    if owner is not None and owner != update.effective_user.id:
        await msg.reply_text(
            f"@{handle} is already registered by someone else. If it's yours, ask an admin for help."
        )
        return ASK_X

    context.user_data["reg"] = {"x_handle": handle, "x_user_id": x_user_id}
    await msg.reply_text(
        f"Got it: @{handle}\n\n<b>Step 2 of 2:</b> send your Verona address. It starts with xion1",
        parse_mode=ParseMode.HTML,
    )
    return ASK_WALLET


async def got_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    wallet = validate_xion_address(msg.text or "")
    if not wallet:
        await msg.reply_text(
            "That isn't a valid Verona address. It should start with xion1. "
            "Copy it again from your wallet and send it."
        )
        return ASK_WALLET
    owner = get_db(context).wallet_owner(wallet)
    if owner is not None and owner != update.effective_user.id:
        await msg.reply_text("That address is already registered by someone else. Send a different one.")
        return ASK_WALLET

    reg = context.user_data.get("reg")
    if not reg:
        return await begin(update, context)
    reg["wallet"] = wallet
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm", callback_data="reg:ok"),
        InlineKeyboardButton("Start over", callback_data="reg:redo"),
    ]])
    await msg.reply_text(
        "Please check your details:\n\n"
        f"X: @{html.escape(reg['x_handle'])}\n"
        f"Verona: <code>{html.escape(wallet)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=buttons,
    )
    return CONFIRM


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data == "reg:redo":
        await query.edit_message_reply_markup(None)
        return await begin(update, context)

    reg = context.user_data.pop("reg", None)
    if not reg or "wallet" not in reg:
        await query.edit_message_text("Something went wrong. Send /register to try again.")
        return ConversationHandler.END

    user = update.effective_user
    db = get_db(context)
    # Check again in case someone else registered the same details meanwhile.
    owner = db.x_owner(reg["x_handle"], reg["x_user_id"])
    wallet_owner = db.wallet_owner(reg["wallet"])
    if (owner not in (None, user.id)) or (wallet_owner not in (None, user.id)):
        await query.edit_message_text("Those details were just registered by someone else. Send /register to try again.")
        return ConversationHandler.END

    db.register_user(user.id, user.username, user.first_name, reg["x_handle"], reg["x_user_id"], reg["wallet"])
    db.link_username_everywhere(user.id, user.username, user.full_name)

    # Their welcome message has done its job.
    for row in db.scheduled_for_user("welcome", user.id):
        await safe_delete(context, row["chat_id"], row["message_id"])
        db.remove_scheduled(row["id"])

    await query.edit_message_text(
        "You're registered!\n\n"
        f"X: @{html.escape(reg['x_handle'])}\n"
        f"Verona: <code>{html.escape(reg['wallet'])}</code>\n\n"
        "Your comments on raids will now be tracked. Send /register again any time to change these.",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reg", None)
    await update.effective_message.reply_text("Registration cancelled. Send /register to start again.")
    return ConversationHandler.END


async def timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reg", None)
    return ConversationHandler.END


async def me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = get_db(context).get_user(update.effective_user.id)
    if not u or not u["registered_at"]:
        await update.effective_message.reply_text("You're not registered yet. Send /register to start.")
        return
    await update.effective_message.reply_text(
        f"X: @{html.escape(u['x_handle'])}\nVerona: <code>{html.escape(u['wallet'])}</code>\n\n"
        "Send /register to change these.",
        parse_mode=ParseMode.HTML,
    )


def build_register_handler() -> ConversationHandler:
    private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
    text = private & filters.TEXT & ~filters.COMMAND
    return ConversationHandler(
        entry_points=[
            CommandHandler("start", start_private, filters=private),
            CommandHandler("register", begin, filters=private),
        ],
        states={
            ASK_X: [MessageHandler(text, got_x)],
            ASK_WALLET: [MessageHandler(text, got_wallet)],
            CONFIRM: [CallbackQueryHandler(confirm, pattern=r"^reg:(ok|redo)$")],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, timeout)],
        },
        fallbacks=[CommandHandler("cancel", cancel, filters=private)],
        allow_reentry=True,
        conversation_timeout=15 * 60,
        name="register",
    )

