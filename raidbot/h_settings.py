"""/settemplate, /setwelcome and /template."""

from telegram import ForceReply, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from .common import get_db, is_admin, is_group, reply_temp
from .h_members import DEFAULT_WELCOME
from .utils import render_template

WAIT_TEMPLATE, WAIT_WELCOME = range(2)
SAMPLE_LINK = "https://x.com/example/status/1234567890123456789"


async def settemplate_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return ConversationHandler.END
    await msg.reply_text(
        "Send your raid template now, as a reply to this message.\n"
        "Put {link} where the X post link should go. Bold, italics and links are kept.\n"
        "Send /cancel to stop.",
        reply_markup=ForceReply(selective=True),
    )
    return WAIT_TEMPLATE


async def settemplate_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    template = msg.text_html
    if "{link}" not in template:
        await msg.reply_text(
            "Your template has no {link}, so the post link would be missing. "
            "Add {link} and send it again, or /cancel.",
            reply_markup=ForceReply(selective=True),
        )
        return WAIT_TEMPLATE
    get_db(context).set_group(msg.chat_id, "template_html", template)
    await msg.reply_text("Template saved. Here's how it will look:")
    await _send_preview(context, msg.chat_id, render_template(template, SAMPLE_LINK))
    return ConversationHandler.END


async def setwelcome_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return ConversationHandler.END
    await msg.reply_text(
        "Send the welcome message now, as a reply to this message.\n"
        "Put {name} where the new member's name should go. A Register button is added automatically.\n"
        "Send /cancel to stop.",
        reply_markup=ForceReply(selective=True),
    )
    return WAIT_WELCOME


async def setwelcome_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    get_db(context).set_group(msg.chat_id, "welcome_html", msg.text_html)
    await msg.reply_text("Welcome message saved. Here's how it will look:")
    await _send_preview(context, msg.chat_id, msg.text_html.replace("{name}", "NewMember"))
    return ConversationHandler.END


async def settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Cancelled. Nothing was changed.")
    return ConversationHandler.END


async def template_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    group = get_db(context).get_group(msg.chat_id)
    if not group or not group["template_html"]:
        await reply_temp(context, msg, "There's no raid template yet. Set one with /settemplate.")
        return
    await msg.reply_text("Current raid template, with an example link:")
    await _send_preview(context, msg.chat_id, render_template(group["template_html"], SAMPLE_LINK))
    if group["welcome_html"]:
        await msg.reply_text("Current welcome message:")
        await _send_preview(context, msg.chat_id, group["welcome_html"].replace("{name}", "NewMember"))
    else:
        await msg.reply_text("Welcome message: using the default. Change it with /setwelcome.")


async def _send_preview(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text_html: str) -> None:
    try:
        await context.bot.send_message(chat_id, text_html, parse_mode=ParseMode.HTML)
    except TelegramError:
        await context.bot.send_message(chat_id, "I couldn't show that preview. Try sending the text again.")


def build_settings_handler() -> ConversationHandler:
    groups = filters.ChatType.GROUPS & filters.UpdateType.MESSAGE
    text = groups & filters.TEXT & ~filters.COMMAND
    return ConversationHandler(
        entry_points=[
            CommandHandler("settemplate", settemplate_start, filters=groups),
            CommandHandler("setwelcome", setwelcome_start, filters=groups),
        ],
        states={
            WAIT_TEMPLATE: [MessageHandler(text, settemplate_save)],
            WAIT_WELCOME: [MessageHandler(text, setwelcome_save)],
        },
        fallbacks=[CommandHandler("cancel", settings_cancel, filters=groups)],
        allow_reentry=True,
        conversation_timeout=10 * 60,
        name="settings",
    )


__all__ = ["build_settings_handler", "template_cmd", "DEFAULT_WELCOME"]
