"""Named raid templates (/settemplate, /templates, /template, /deltemplate) and /setgreet.

Admins type the commands in the group; the setup, lists and previews happen in
their private chat with the bot, so none of it shows in the group.
"""

import html
import re

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from .common import (
    MEDIUM, bot_link, delete_later, get_db, is_admin, is_group, private_target, reply_temp, tell_admin,
    tell_admin_id,
)
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


async def _preview(context: ContextTypes.DEFAULT_TYPE, chat_id: int, tpl, private: bool) -> None:
    """Show a template filled in with an example post. In the group it deletes itself after a minute."""
    text = render_template(tpl["body_html"], SAMPLE_LINK, SAMPLE_XNAME, SAMPLE_XHANDLE)
    try:
        _, sent = await post_template(context, chat_id, text, tpl["photo_file_id"])
    except TelegramError:
        name = html.escape(tpl["name"])
        await context.bot.send_message(chat_id, f"I couldn't show the '{name}' template. "
                                                f"Set it again with /settemplate {name}.")
        return
    if not private:
        for m in sent:
            delete_later(context, chat_id, m.message_id, MEDIUM)


async def _show_template(context: ContextTypes.DEFAULT_TYPE, message: Message, user_id: int | None,
                         group_id: int, intro: str, tpl) -> None:
    """Send the intro and a preview to the admin privately, or briefly in the group if that's not possible."""
    private = await tell_admin_id(context, group_id, user_id, intro, MEDIUM,
                                  group_title=message.chat.title if message.chat.id == group_id else None)
    await _preview(context, user_id if private else group_id, tpl, private)


# ---------------------------------------------------------------- setup in a private chat
# /settemplate and /setgreet are typed in the group, then the rest happens in the
# admin's private chat with the bot, so nothing about the setup shows in the group.

def _forget(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ("setup_chat", "setup_title", "tpl_name"):
        context.user_data.pop(key, None)


async def _start_private_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> bool:
    """Ask for the template or welcome message in the admin's private chat. False if that's not possible."""
    msg = update.effective_message
    user_id = private_target(msg)
    title = update.effective_chat.title or "your group"
    if user_id is None:
        await reply_temp(context, msg, "I set this up in a private chat, so turn off 'Remain anonymous' "
                                       "and run the command again.")
        return False
    try:
        await context.bot.send_message(user_id, f"<b>{html.escape(title)}</b>\n{prompt}", parse_mode=ParseMode.HTML)
    except TelegramError:
        await reply_temp(context, msg, f'I set this up in a private chat. <a href="{bot_link(context)}">Press Start '
                                       "on me</a>, then run the command again in the group.")
        return False
    context.user_data["setup_chat"] = msg.chat_id
    context.user_data["setup_title"] = title
    return True


async def _setup_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """The group being set up, if the person is still one of its admins."""
    chat_id = context.user_data.get("setup_chat")
    if chat_id is None or not await is_admin(context, chat_id, user_id=update.effective_user.id):
        return None
    return chat_id


# ---------------------------------------------------------------- /settemplate

async def settemplate_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return ConversationHandler.END
    name = (context.args[0].lower() if context.args else DEFAULT_TEMPLATE)
    if not valid_template_name(name):
        await tell_admin(context, msg, "Template names can use letters, numbers and _ (max 20). Example: /settemplate fun")
        return ConversationHandler.END
    _forget(context)
    exists = get_db(context).get_template(msg.chat_id, name)
    prompt = (
        f"{'Replacing' if exists else 'New'} template: {html.escape(name)}\n\n"
        "Send it to me here now. Send plain text, or a photo with the template as "
        "its caption to attach that image.\n\n"
        f"You can use:\n{html.escape(VARIABLES_HELP)}\n\nSend /cancel to stop."
    )
    if not await _start_private_setup(update, context, prompt):
        return ConversationHandler.END
    context.user_data["tpl_name"] = name
    return WAIT_TEMPLATE


async def settemplate_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    chat_id = await _setup_group(update, context)
    if chat_id is None:
        _forget(context)
        await msg.reply_text("You're no longer an admin of that group, so nothing was saved.")
        return ConversationHandler.END
    name = context.user_data.get("tpl_name", DEFAULT_TEMPLATE)
    photo_id = msg.photo[-1].file_id if msg.photo else None
    body = (msg.caption_html if msg.photo else msg.text_html) or ""
    if "{link}" not in body:
        await msg.reply_text("That template has no {link}, so the post link would be missing. "
                             "Add {link} and send it again, or /cancel.")
        return WAIT_TEMPLATE
    db = get_db(context)
    db.save_template(chat_id, name, body, photo_id)
    _forget(context)
    extra = " with its image" if photo_id else ""
    await msg.reply_text(f"Saved template <b>{html.escape(name)}</b>{extra}. Use it with "
                         f"<code>@raidall {html.escape(name)}</code>. Preview:", parse_mode=ParseMode.HTML)
    await _preview(context, msg.chat_id, db.get_template(chat_id, name), private=True)
    return ConversationHandler.END


# ---------------------------------------------------------------- /setgreet

async def setwelcome_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return ConversationHandler.END
    _forget(context)
    prompt = (
        "Send me the welcome message here now.\n"
        "Put {name} where the new member's name should go. A Register button is added automatically.\n"
        "Send /cancel to stop."
    )
    if not await _start_private_setup(update, context, prompt):
        return ConversationHandler.END
    return WAIT_WELCOME


async def setwelcome_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.effective_message
    chat_id = await _setup_group(update, context)
    _forget(context)
    if chat_id is None:
        await msg.reply_text("You're no longer an admin of that group, so nothing was saved.")
        return ConversationHandler.END
    get_db(context).set_group(chat_id, "welcome_html", msg.text_html)
    await msg.reply_text("Welcome message saved. Preview:")
    await msg.reply_text(msg.text_html.replace("{name}", "NewMember"), parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def settings_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _forget(context)
    msg = update.effective_message
    if is_group(update):
        await tell_admin(context, msg, "Cancelled. Nothing was changed.")
    else:
        await msg.reply_text("Cancelled. Nothing was changed.")
    return ConversationHandler.END


async def settings_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _forget(context)
    return ConversationHandler.END


# ---------------------------------------------------------------- listing, preview, delete

async def templates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    db = get_db(context)
    rows = db.list_templates(msg.chat_id)
    if not rows:
        await tell_admin(context, msg, "No raid templates yet. Make one with /settemplate or /settemplate fun.")
        return
    lines = ["<b>Raid templates</b>"]
    for t in rows:
        note = " (with image)" if t["photo_file_id"] else ""
        how = "@raidall" if t["name"] == DEFAULT_TEMPLATE else f"@raidall {t['name']}"
        lines.append(f"{html.escape(t['name'])}{note}: <code>{html.escape(how)}</code>")
    group = db.get_group(msg.chat_id)
    lines += ["", "Welcome message: " + ("custom" if group and group["welcome_html"] else "default"),
              "", "Preview one with /template name"]
    await tell_admin(context, msg, "\n".join(lines), MEDIUM)


async def template_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    name = context.args[0].lower() if context.args else DEFAULT_TEMPLATE
    tpl = get_db(context).get_template(msg.chat_id, name)
    if not tpl:
        await tell_admin(context, msg, f"There's no template called {html.escape(name)}. See them all with /templates.")
        return
    await _show_template(context, msg, private_target(msg), msg.chat_id,
                         f"Template <b>{html.escape(name)}</b>, with an example post by {SAMPLE_XNAME}:", tpl)


async def deltemplate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not is_group(update) or not await is_admin(context, msg.chat_id, msg):
        return
    if not context.args:
        await tell_admin(context, msg, "Usage: /deltemplate name")
        return
    name = context.args[0].lower()
    if get_db(context).delete_template(msg.chat_id, name):
        await tell_admin(context, msg, f"Deleted template {html.escape(name)}.")
    else:
        await tell_admin(context, msg, f"There's no template called {html.escape(name)}.")


def build_settings_handler() -> ConversationHandler:
    groups = filters.ChatType.GROUPS & filters.UpdateType.MESSAGE
    private = filters.ChatType.PRIVATE & filters.UpdateType.MESSAGE
    text = private & filters.TEXT & ~filters.COMMAND
    return ConversationHandler(
        entry_points=[
            CommandHandler("settemplate", settemplate_start, filters=groups),
            CommandHandler("setgreet", setwelcome_start, filters=groups),
        ],
        states={
            WAIT_TEMPLATE: [MessageHandler(text | (private & filters.PHOTO), settemplate_save)],
            WAIT_WELCOME: [MessageHandler(text, setwelcome_save)],
            ConversationHandler.TIMEOUT: [MessageHandler(filters.ALL, settings_timeout)],
        },
        fallbacks=[CommandHandler("cancel", settings_cancel, filters=groups | private)],
        allow_reentry=True,
        conversation_timeout=10 * 60,
        # Keyed by person, not chat: the command is typed in the group and answered in private.
        per_chat=False,
        name="settings",
    )
