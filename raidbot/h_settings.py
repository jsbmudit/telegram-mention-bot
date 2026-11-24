"""Named raid templates (/settemplate, /templates, /template, /deltemplate) and /setgreet.

Setup chatter cleans itself up: the bot's prompts and the admin's own template
message are deleted once the template is saved, and previews delete after a minute.
"""

import html
import re

from telegram import ForceReply, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from .common import MEDIUM, SHORT, delete_later, get_db, is_admin, is_group, reply_temp, send_temp
from .db import DEFAULT_TEMPLATE
from .utils import render_template, valid_template_name

WAIT_TEMPLATE, WAIT_WELCOME = range(2)
SAMPLE_LINK = "https://x.com/andyyy/status/1234567890123456789"
SAMPLE_XNAME, SAMPLE_XHANDLE = "Andyyy", "andyyy"
CAPTION_LIMIT = 1024

VARIABLES_HELP = (
    "{link}: the X post link (required)\n"
    "{xname}: the post author's X display name\n"
    "{xhandle}: their @handle\n"
    "{xprofile}: a link to their X profile"
)


# ---------------------------------------------------------------- sending templates

def _plain_length(text_html: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", text_html)))


async def post_template(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text_html: str,
                        photo_file_id: str | None) -> tuple[Message, list[Message]]:
    """Send a filled-in template, with its image if it has one.

    Returns (message to pin, every message sent). If the text is too long to be
    an image caption, the image goes first on its own and the text follows.
    """
    if photo_file_id:
        if _plain_length(text_html) <= CAPTION_LIMIT:
            try:
                sent = await context.bot.send_photo(
                    chat_id, photo_file_id, caption=text_html, parse_mode=ParseMode.HTML
                )
                return sent, [sent]
            except BadRequest:
                pass  # fall back to image + separate text below
        image = await context.bot.send_photo(chat_id, photo_file_id)
        text = await context.bot.send_message(chat_id, text_html, parse_mode=ParseMode.HTML)
        return text, [image, text]
    text = await context.bot.send_message(chat_id, text_html, parse_mode=ParseMode.HTML)
    return text, [text]


async def _preview(context: ContextTypes.DEFAULT_TYPE, chat_id: int, tpl) -> None:
    text = render_template(tpl["body_html"], SAMPLE_LINK, SAMPLE_XNAME, SAMPLE_XHANDLE)
    try:
        _, sent = await post_template(context, chat_id, text, tpl["photo_file_id"])
    except TelegramError:
        name = html.escape(tpl["name"])
        await send_temp(context, chat_id, f"I couldn't show the '{name}' template. Set it again with /settemplate {name}.")
        return
    for m in sent:
        delete_later(context, chat_id, m.message_id, MEDIUM)


# ---------------------------------------------------------------- conversation helpers

def _track(context: ContextTypes.DEFAULT_TYPE, *messages: Message | None) -> None:
    """Remember setup messages so they can all be cleaned up at the end."""
    bucket = context.user_data.setdefault("setup_msgs", [])
    for m in messages:
        if m is not None:
            bucket.append((m.chat_id, m.message_id))


def _cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    for chat_id, message_id in context.user_data.pop("setup_msgs", []):
        delete_later(context, chat_id, message_id, 1)
    context.user_data.pop("tpl_name", None)


# ---------------------------------------------------------------- /settemplate

async def settemplate_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return ConversationHandler.END
    name = (context.args[0].lower() if context.args else DEFAULT_TEMPLATE)
    if not valid_template_name(name):
        await reply_temp(context, msg, "Template names can use letters, numbers and _ (max 20). Example: /settemplate fun")
        return ConversationHandler.END
    _cleanup(context)
    context.user_data["tpl_name"] = name
    exists = get_db(context).get_template(msg.chat_id, name)
    prompt = await msg.reply_text(
        f"{'Replacing' if exists else 'New'} template: {name}\n\n"
        "Send it now as a reply to this message. Send plain text, or a photo with the template as "
        "its caption to attach that image.\n\n"
        f"You can use:\n{VARIABLES_HELP}\n\nSend /cancel to stop.",
        reply_markup=ForceReply(selective=True),
    )
    _track(context, prompt)
    return WAIT_TEMPLATE


async def settemplate_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    _track(context, msg)
    name = context.user_data.get("tpl_name", DEFAULT_TEMPLATE)
    photo_id = msg.photo[-1].file_id if msg.photo else None
    body = (msg.caption_html if msg.photo else msg.text_html) or ""
    if "{link}" not in body:
        warn = await msg.reply_text(
            "That template has no {link}, so the post link would be missing. "
            "Add {link} and send it again, or /cancel.",
            reply_markup=ForceReply(selective=True),
        )
        _track(context, warn)
        return WAIT_TEMPLATE
    db = get_db(context)
    db.save_template(msg.chat_id, name, body, photo_id)
    _cleanup(context)
    extra = " with its image" if photo_id else ""
    await send_temp(context, msg.chat_id,
                    f"Saved template <b>{html.escape(name)}</b>{extra}. Use it with "
                    f"<code>@raidall {html.escape(name)}</code>. Preview:", MEDIUM)
    await _preview(context, msg.chat_id, db.get_template(msg.chat_id, name))
    return ConversationHandler.END


# ---------------------------------------------------------------- /setgreet

async def setwelcome_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return ConversationHandler.END
    _cleanup(context)
    prompt = await msg.reply_text(
        "Send the welcome message now, as a reply to this message.\n"
        "Put {name} where the new member's name should go. A Register button is added automatically.\n"
        "Send /cancel to stop.",
        reply_markup=ForceReply(selective=True),
    )
    _track(context, prompt)
    return WAIT_WELCOME


async def setwelcome_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    _track(context, msg)
    get_db(context).set_group(msg.chat_id, "welcome_html", msg.text_html)
    _cleanup(context)
    await send_temp(context, msg.chat_id, "Welcome message saved. Preview:", MEDIUM)
    await send_temp(context, msg.chat_id, msg.text_html.replace("{name}", "NewMember"), MEDIUM)
    return ConversationHandler.END


async def settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _cleanup(context)
    await reply_temp(context, update.effective_message, "Cancelled. Nothing was changed.")
    return ConversationHandler.END


async def settings_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _cleanup(context)
    return ConversationHandler.END


# ---------------------------------------------------------------- listing, preview, delete

async def templates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    db = get_db(context)
    rows = db.list_templates(msg.chat_id)
    if not rows:
        await reply_temp(context, msg, "No raid templates yet. Make one with /settemplate or /settemplate fun.")
        return
    lines = ["<b>Raid templates</b>"]
    for t in rows:
        note = " (with image)" if t["photo_file_id"] else ""
        how = "@raidall" if t["name"] == DEFAULT_TEMPLATE else f"@raidall {t['name']}"
        lines.append(f"{html.escape(t['name'])}{note}: <code>{html.escape(how)}</code>")
    group = db.get_group(msg.chat_id)
    lines += ["", "Welcome message: " + ("custom" if group and group["welcome_html"] else "default"),
              "", "Preview one with /template name"]
    await reply_temp(context, msg, "\n".join(lines), MEDIUM)


async def template_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    name = context.args[0].lower() if context.args else DEFAULT_TEMPLATE
    tpl = get_db(context).get_template(msg.chat_id, name)
    if not tpl:
        await reply_temp(context, msg, f"There's no template called {html.escape(name)}. See them all with /templates.")
        return
    await send_temp(context, msg.chat_id,
                    f"Template <b>{html.escape(name)}</b>, with an example post by {SAMPLE_XNAME}:", MEDIUM)
    await _preview(context, msg.chat_id, tpl)


async def deltemplate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    if not context.args:
        await reply_temp(context, msg, "Usage: /deltemplate name")
        return
    name = context.args[0].lower()
    if get_db(context).delete_template(msg.chat_id, name):
        await reply_temp(context, msg, f"Deleted template {html.escape(name)}.")
    else:
        await reply_temp(context, msg, f"There's no template called {html.escape(name)}.")


def build_settings_handler() -> ConversationHandler:
    groups = filters.ChatType.GROUPS & filters.UpdateType.MESSAGE
    text = groups & filters.TEXT & ~filters.COMMAND
    return ConversationHandler(
        entry_points=[
            CommandHandler("settemplate", settemplate_start, filters=groups),
            CommandHandler("setgreet", setwelcome_start, filters=groups),
        ],
        states={
            WAIT_TEMPLATE: [MessageHandler(text | (groups & filters.PHOTO), settemplate_save)],
            WAIT_WELCOME: [MessageHandler(text, setwelcome_save)],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, settings_timeout)],
        },
        fallbacks=[CommandHandler("cancel", settings_cancel, filters=groups)],
        allow_reentry=True,
        conversation_timeout=10 * 60,
        name="settings",
    )
