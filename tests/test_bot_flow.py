"""End-to-end test: the real bot handlers talking to a fake Telegram and a fake X.

Nothing here touches the network or a real group.
"""

import asyncio
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from telegram import Update
from telegram.ext import CallbackContext
from telegram.request import BaseRequest

from raidbot.app import build_app, delete_job, housekeeping_job
from raidbot.h_feed import feed_job
from raidbot.config import Config
from raidbot.h_raid import tracking_job
from raidbot.utils import bech32_encode, convert_bits
from raidbot.xapi import Post, PostAuthor, Reply

BOT_ID = 5000
ADMIN = {"id": 1, "is_bot": False, "first_name": "Admin", "username": "boss"}
ALICE = {"id": 11, "is_bot": False, "first_name": "Alice", "username": "alice"}
BOB = {"id": 12, "is_bot": False, "first_name": "Bob", "username": "bobby"}
NEWBIE = {"id": 13, "is_bot": False, "first_name": "Newbie"}
STRANGER = {"id": 14, "is_bot": False, "first_name": "Rando", "username": "rando"}
CHAT = {"id": -100777, "type": "supergroup", "title": "Raid Group"}
POST = "https://x.com/Burnt/status/1861234567890123456"
FAKE_TOKEN = "123456789:" + "fake" * 9  # not a real token


def wallet(n: int) -> str:
    return bech32_encode("xion", convert_bits([n] * 32, 8, 5, True))


class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.files: list[bytes] = []
        self.ids = itertools.count(100)

    @property
    def read_timeout(self):
        return 1.0

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    def of(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]

    def texts(self) -> list[str]:
        return [p.get("text", "") for p in self.of("sendMessage")]

    def clear(self):
        self.calls.clear()

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        if request_data and request_data.contains_files:
            for f in request_data.multipart_data.values():
                content = f[1] if isinstance(f, tuple) else f
                self.files.append(content)
        self.calls.append((name, params))
        return 200, json.dumps({"ok": True, "result": self.result(name, params)}).encode()

    def result(self, name: str, p: dict):
        if name == "getMe":
            return {"id": BOT_ID, "is_bot": True, "first_name": "Raid", "username": "raid_test_bot",
                    "can_join_groups": True, "can_read_all_group_messages": True,
                    "supports_inline_queries": False}
        if name in ("sendMessage", "sendDocument", "sendPhoto", "editMessageText", "editMessageReplyMarkup"):
            chat_id = int(p.get("chat_id", CHAT["id"]))
            chat = CHAT if chat_id < 0 else {"id": chat_id, "type": "private", "first_name": "x"}
            msg_id = int(p["message_id"]) if "message_id" in p else next(self.ids)
            out = {"message_id": msg_id, "date": int(time.time()), "chat": chat,
                   "from": {"id": BOT_ID, "is_bot": True, "first_name": "Raid"}}
            if name == "sendPhoto":
                out["photo"] = [{"file_id": str(p.get("photo")), "file_unique_id": "u", "width": 1, "height": 1}]
                out["caption"] = p.get("caption", "")
            else:
                out["text"] = p.get("text", "")
            return out
        if name == "getChatAdministrators":
            return [{"status": "creator", "user": ADMIN, "is_anonymous": False}]
        if name == "getChat":
            return {**CHAT, "accent_color_id": 0, "max_reaction_count": 11,
                    "accepted_gift_types": {"unlimited_gifts": False, "limited_gifts": False,
                                            "unique_gifts": False, "premium_subscription": False,
                                            "gifts_from_channels": False},
                    "permissions": {"can_send_messages": True, "can_send_photos": False}}
        return True


class FakeX:
    def __init__(self):
        self.replies: list[Reply] = []
        self.users = {"alicex": ("111", "AliceX"), "bobx": ("222", "BobX")}

    async def get_user(self, username):
        self.user_lookups = getattr(self, "user_lookups", 0) + 1
        return self.users.get(username.lower())

    async def get_conversation_id(self, tweet_id):
        return tweet_id

    timeline: list = []

    async def lookup_user(self, username):
        if username.lower() == "veronaofficial":
            return {"id": "4242", "username": "VeronaOfficial", "name": "Verona"}
        return None

    async def user_posts(self, user_id, since_id=None, include_all=False, max_results=100, max_pages=3):
        self.timeline_calls = getattr(self, "timeline_calls", 0) + 1
        posts = [p for p in self.timeline if include_all or p.kind in ("post", "quote")]
        if since_id:
            posts = [p for p in posts if int(p.id) > int(since_id)]
        return sorted(posts, key=lambda p: -int(p.id))[:max_results]

    async def get_post_info(self, tweet_id):
        self.post_info_calls = getattr(self, "post_info_calls", 0) + 1
        return tweet_id, PostAuthor(name="Andyyy", username="andyyy")

    async def search_replies(self, conversation_id, since_id=None):
        return list(self.replies), None

    async def close(self):
        pass


async def _no_oembed(tweet_id, handle=None):
    return None


class Harness:
    def __init__(self, tmp_path):
        import raidbot.h_raid
        raidbot.h_raid.oembed_author = _no_oembed  # never call the real X embed service in tests
        self.tg = FakeTelegram()
        cfg = Config(telegram_token=FAKE_TOKEN, x_bearer_token=None, db_path=str(tmp_path / "bot.db"),
                     track_interval=600, lock_check_interval=120)
        self.app = build_app(cfg, request=self.tg, rate_limit=False)
        self.x = FakeX()
        self.app.bot_data["x"] = self.x
        self.db = self.app.bot_data["db"]
        self.ids = itertools.count(1)
        self.ctx = None

    async def start(self):
        await self.app.initialize()
        self.ctx = CallbackContext(self.app)

    async def send(self, user, text, chat=CHAT, reply_to=None, edited=False, **extra):
        msg = {"message_id": next(self.ids), "date": int(time.time()), "chat": chat, "from": user, **extra}
        if "photo" not in extra:
            msg["text"] = text
        self.last_id = msg["message_id"]
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if reply_to:
            msg["reply_to_message"] = reply_to
        key = "edited_message" if edited else "message"
        if edited:
            msg["edit_date"] = int(time.time())
        await self.app.process_update(Update.de_json({"update_id": next(self.ids), key: msg}, self.app.bot))
        await self.settle()

    async def dm(self, user, text):
        await self.send(user, text, chat={"id": user["id"], "type": "private", "first_name": user["first_name"]})

    async def press(self, user, data, chat):
        bot_msg = {"message_id": 1, "date": int(time.time()), "chat": chat,
                   "from": {"id": BOT_ID, "is_bot": True, "first_name": "Raid"}, "text": "x"}
        upd = {"update_id": next(self.ids),
               "callback_query": {"id": str(next(self.ids)), "from": user, "chat_instance": "c",
                                  "data": data, "message": bot_msg}}
        await self.app.process_update(Update.de_json(upd, self.app.bot))
        await self.settle()

    async def settle(self):
        for _ in range(5):
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
            if not tasks:
                return
            await asyncio.wait(tasks, timeout=2)

    async def register(self, user, x_handle, w):
        await self.dm(user, "/start register")
        await self.dm(user, x_handle)
        await self.dm(user, w)
        await self.press(user, "reg:ok", {"id": user["id"], "type": "private", "first_name": user["first_name"]})


def test_full_raid_flow(tmp_path):
    asyncio.run(_full_raid_flow(tmp_path))


async def _full_raid_flow(tmp_path):
    h = Harness(tmp_path)
    tg, db = h.tg, h.db
    await h.start()

    # 1. Admin preloads usernames. Non-admins can't.
    await h.send(STRANGER, "/addusers @hacker")
    assert not any("Added" in t for t in tg.texts())
    await h.send(ADMIN, "/addusers @alice @bobby @carol @daveo @erinn @frank @x")
    assert any("Added 6" in t and "Skipped (not valid Telegram usernames): @x" in t for t in tg.texts())

    # 2. Alice talks, so her preloaded row gets her user ID.
    await h.send(ALICE, "gm")
    assert any(m["user_id"] == 11 for m in db.list_members(CHAT["id"]))

    # 3. Alice registers privately: X link gets normalised, wallet checked, then confirmed.
    tg.clear()
    await h.dm(ALICE, "/start register")
    await h.dm(ALICE, "https://x.com/alicex")
    await h.dm(ALICE, "xion1notvalid")
    assert any("isn't a valid Verona address" in t for t in tg.texts())
    await h.dm(ALICE, wallet(1))
    await h.press(ALICE, "reg:ok", {"id": 11, "type": "private", "first_name": "Alice"})
    alice = db.get_user(11)
    assert alice["x_handle"] == "AliceX" and alice["x_user_id"] == "111" and alice["wallet"] == wallet(1)

    # Bob can't claim Alice's X account.
    tg.clear()
    await h.dm(BOB, "/register")
    await h.dm(BOB, "@AliceX")
    assert any("already registered by someone else" in t for t in tg.texts())
    await h.dm(BOB, "/cancel")

    # 4. Admin sets the raid template; one without {link} is refused.
    await h.send(ADMIN, "/settemplate")
    await h.send(ADMIN, "no link here")
    assert db.get_template(CHAT["id"], "default") is None
    await h.send(ADMIN, "RAID NOW {link} like and comment")
    assert "{link}" in db.get_template(CHAT["id"], "default")["body_html"]

    # 5. Admin posts the link, then @raidall: tags (registered first), raid message, pin.
    await h.send(ADMIN, f"new post {POST}?s=20")
    tg.clear()
    await h.send(ADMIN, "@raidall")
    texts = tg.texts()
    assert "@alice" in texts[0] and "UNREGISTERED" not in texts[0]
    unreg = [t for t in texts if t.startswith("<b>UNREGISTERED USERS:</b>")]
    # The admin and the stranger were auto-added when they spoke: 7 unregistered, 2 batches of max 5.
    assert len(unreg) == 2 and "@bobby" in unreg[0] and "@frank" in unreg[1]
    assert all(t.count("@") <= 5 for t in texts[:-1])
    assert texts[-1] == f"RAID NOW {POST} like and comment"
    pins = tg.of("pinChatMessage")
    assert len(pins) == 1 and not pins[0].get("disable_notification")
    raid = db.latest_raid(CHAT["id"])
    assert raid["raid_no"] == 1 and raid["status"] == "active"
    tag_deletes = db.due_deletes(time.time() + 4 * 3600 + 5)
    assert len([d for d in tag_deletes if d["kind"] == "tag"]) == 3

    # 6. Cooldown, edited messages and non-admins don't trigger tagging.
    tg.clear()
    await h.send(ADMIN, "@all")
    assert any("cooldown" in t for t in tg.texts())
    tg.clear()
    await h.send(ADMIN, "@all", edited=True)
    await h.send(STRANGER, "@all")
    assert tg.of("sendMessage") == []

    # 7. Tracking: Alice's reply counts, an outsider's doesn't.
    t0 = raid["started_at"]
    h.x.replies = [Reply("9001", "111", "AliceX", t0 + 75), Reply("9002", "777", "Outsider", t0 + 10)]
    await tracking_job(h.ctx)
    assert db.engagement_count(raid["id"]) == 1

    # 8. Lock until 2 comments. Bob registers and comments, lock opens on the next check.
    tg.clear()
    await h.send(ADMIN, "/raidlock 2 60m")
    perms = tg.of("setChatPermissions")
    assert len(perms) == 1 and "Chat locked" in tg.texts()[-1]
    await h.register(BOB, "bobx", wallet(2))
    assert db.get_user(12)["registered_at"]
    h.x.replies.append(Reply("9003", "222", "BobX", t0 + 400))
    db.update_raid(raid["id"], last_checked_at=0)
    tg.clear()
    await tracking_job(h.ctx)
    perms = tg.of("setChatPermissions")
    assert len(perms) == 1
    restored = perms[0]["permissions"]
    restored = json.loads(restored) if isinstance(restored, str) else restored
    assert restored.get("can_send_messages") is True and restored.get("can_send_photos") is False
    summary = tg.texts()[-1]
    assert "Chat unlocked: target reached" in summary and "Commented: 2 of 2" in summary
    assert db.active_lock(CHAT["id"]) is None
    unlock_summary_id = db.lock_summaries_for_raid(raid["id"])[0]["summary_message_id"]
    assert unlock_summary_id

    # 9. /raidcsv: command deleted from the group, file sent privately to the admin.
    tg.clear()
    tg.files.clear()
    await h.send(ADMIN, "/raidcsv")
    assert tg.of("deleteMessage")
    docs = tg.of("sendDocument")
    assert len(docs) == 1 and int(docs[0]["chat_id"]) == ADMIN["id"]
    csv_text = b"".join(f if isinstance(f, bytes) else b"" for f in tg.files).decode("utf-8-sig", "ignore")
    assert "https://x.com/AliceX" in csv_text and wallet(1) in csv_text and "1m 15s" in csv_text
    assert csv_text.count(POST) == 1  # the raid link is listed once, not on every row

    # 10. A second raid can run alongside; /endraid then asks which one.
    db.set_group(CHAT["id"], "last_tag_at", 0)
    await h.send(ADMIN, "@raidall https://x.com/Burnt/status/1861234567890999999")
    assert len(db.active_raids(CHAT["id"])) == 2
    tg.clear()
    await h.send(ADMIN, "/endraid")
    assert tg.of("sendMessage")[-1].get("reply_markup")
    await h.press(ADMIN, f"endraid:{raid['id']}", CHAT)
    assert db.get_raid(raid["id"])["status"] == "ended"
    assert any("Raid #1 is over" in t for t in tg.texts())
    # The final summary replaces the earlier "Chat unlocked" summary.
    assert unlock_summary_id in [int(p["message_id"]) for p in tg.of("deleteMessage")]
    assert db.lock_summaries_for_raid(raid["id"]) == []

    # 11. Raids end on their own after 12 hours.
    db.conn.execute("UPDATE raids SET ends_at = 0 WHERE status = 'active'")
    db.conn.commit()
    await housekeeping_job(h.ctx)
    assert db.active_raids(CHAT["id"]) == []

    # 12. Welcome: posted on join, deleted as soon as the newcomer registers.
    tg.clear()
    await h.send(ADMIN, "", new_chat_members=[NEWBIE])
    welcome = [p for p in tg.of("sendMessage") if "Welcome" in p.get("text", "")]
    assert len(welcome) == 1 and "tg://user?id=13" in welcome[0]["text"]
    assert any(m["user_id"] == 13 for m in db.list_members(CHAT["id"]))
    tg.clear()
    h.x.users["newbiex_1"] = ("333", "NewbieX_1")
    await h.register(NEWBIE, "newbiex_1", wallet(3))
    assert tg.of("deleteMessage"), "welcome message should be deleted after registering"

    # 13. Leaving removes the member.
    await h.send(ADMIN, "", left_chat_member=NEWBIE)
    assert not any(m["user_id"] == 13 for m in db.list_members(CHAT["id"]))

    await h.app.shutdown()


def test_missing_token_message(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("TELEGRAM_BOT_TOKEN", "X_BEARER_TOKEN")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    r = subprocess.run([sys.executable, "-m", "raidbot"], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert "TELEGRAM_BOT_TOKEN is not set" in r.stderr
    assert "Traceback" not in r.stderr


def test_cleanup_and_named_templates(tmp_path):
    asyncio.run(_cleanup_and_named_templates(tmp_path))


def _scheduled(db, message_id):
    return db.conn.execute("SELECT * FROM scheduled_deletes WHERE message_id = ?", (message_id,)).fetchall()


async def _cleanup_and_named_templates(tmp_path):
    h = Harness(tmp_path)
    tg, db = h.tg, h.db
    await h.start()
    chat_id = CHAT["id"]

    # Admin commands and their names are removed from the chat a few seconds later.
    await h.send(ADMIN, "/addusers @alice @bobby")
    cmd_rows = _scheduled(db, h.last_id)
    assert cmd_rows and cmd_rows[0]["delete_at"] - time.time() < 5
    reply = [p for p in tg.of("sendMessage") if "Added" in p["text"]][0]
    # The bot's answer goes too, after about 15 seconds.
    assert len(db.due_deletes(time.time() + 16)) >= 2

    # A command for some other bot is left alone.
    await h.send(ADMIN, "/start@someotherbot")
    assert _scheduled(db, h.last_id) == []

    # /register: the prompt disappears when someone opens the bot through it.
    tg.clear()
    await h.send(ALICE, "/register")
    edits = tg.of("editMessageReplyMarkup")
    assert len(edits) == 1
    markup = edits[0]["reply_markup"]
    markup = json.loads(markup) if isinstance(markup, str) else markup
    url = markup["inline_keyboard"][0][0]["url"]
    payload = url.split("?start=")[1]
    prompt_id = int(edits[0]["message_id"])
    assert payload == f"reg{chat_id}_{prompt_id}"
    tg.clear()
    await h.dm(ALICE, f"/start {payload}")
    assert [int(p["message_id"]) for p in tg.of("deleteMessage")] == [prompt_id]
    await h.dm(ALICE, "/cancel")
    # A hand-made link can't make the bot delete other messages.
    tg.clear()
    await h.dm(ALICE, f"/start reg{chat_id}_5")
    assert tg.of("deleteMessage") == []
    await h.dm(ALICE, "/cancel")

    # Named template with an image and {xname}/{xprofile}.
    await h.send(ADMIN, "/settemplate fun")
    tg.clear()
    await h.send(ADMIN, "", photo=[{"file_id": "PHOTO_FUN", "file_unique_id": "pf", "width": 9, "height": 9}],
                 caption="Our dear friend {xname} is posting, show him how Verona bulls are! {link} {xprofile}")
    tpl = db.get_template(chat_id, "fun")
    assert tpl["photo_file_id"] == "PHOTO_FUN"
    # The setup prompt and the admin's own template message get cleaned up.
    assert _scheduled(db, h.last_id)
    previews = tg.of("sendPhoto")
    assert previews and "Andyyy" in previews[0]["caption"]

    await h.send(ADMIN, "/settemplate")
    await h.send(ADMIN, "Plain raid {link}")
    assert db.get_template(chat_id, "default")

    # Unknown template name: a short error, nothing posted.
    tg.clear()
    await h.send(ADMIN, "@raidall nope")
    assert any("no template called nope" in t for t in tg.texts())
    assert tg.of("sendPhoto") == [] and tg.of("pinChatMessage") == []

    # @raidall fun <link>: tags, then the image with the filled-in caption, pinned.
    tg.clear()
    await h.send(ADMIN, "@raidall fun https://x.com/andyyy/status/1861234567890555555")
    trigger_id = h.last_id
    photos = tg.of("sendPhoto")
    assert len(photos) == 1 and photos[0]["photo"] == "PHOTO_FUN"
    caption = photos[0]["caption"]
    assert "Our dear friend Andyyy is posting" in caption
    assert "https://x.com/andyyy/status/1861234567890555555" in caption and "https://x.com/andyyy " not in caption
    assert "https://x.com/andyyy" in caption
    raid = db.latest_raid(chat_id)
    assert [int(p["message_id"]) for p in tg.of("pinChatMessage")] == [raid["message_id"]]
    # "@raidall fun <link>" on its own is a pure command, so it gets tidied away.
    assert _scheduled(db, trigger_id)

    # "@all" with a real message around it is left alone.
    db.set_group(chat_id, "last_tag_at", 0)
    await h.send(ADMIN, "@all gm fam, call in 10")
    assert _scheduled(db, h.last_id) == []

    # Lock notice is removed when the chat unlocks; the summary stays.
    tg.clear()
    await h.send(ADMIN, "/raidlock 5 30m")
    notice_id = db.active_lock(chat_id)["message_id"]
    assert notice_id
    tg.clear()
    await h.send(ADMIN, "/raidunlock")
    assert notice_id in [int(p["message_id"]) for p in tg.of("deleteMessage")]
    summary = tg.texts()[-1]
    assert "Chat unlocked" in summary
    assert all(r["message_id"] != notice_id for r in db.due_deletes(time.time() + 10 ** 6))

    # The fast delete job actually removes due messages.
    db.conn.execute("UPDATE scheduled_deletes SET delete_at = 0 WHERE kind = 'temp'")
    db.conn.commit()
    tg.clear()
    await delete_job(h.ctx)
    assert tg.of("deleteMessage")
    assert db.conn.execute("SELECT COUNT(*) FROM scheduled_deletes WHERE kind = 'temp'").fetchone()[0] == 0

    await h.app.shutdown()


def test_x_feed_and_rose_names(tmp_path):
    asyncio.run(_x_feed_and_rose_names(tmp_path))


async def _x_feed_and_rose_names(tmp_path):
    h = Harness(tmp_path)
    tg, db = h.tg, h.db
    await h.start()
    chat_id = CHAT["id"]

    # Rose's /lock is not ours: the bot ignores it and doesn't delete it.
    tg.clear()
    await h.send(ADMIN, "/lock")
    assert tg.calls == [] and _scheduled(db, h.last_id) == []

    # Without the X key the feed explains what's missing.
    h.app.bot_data["x"] = None
    await h.send(ADMIN, "/feedon VeronaOfficial")
    assert any("needs the X API key" in t for t in tg.texts())
    h.app.bot_data["x"] = h.x

    # Turning it on starts from the newest post, so old posts aren't dumped in.
    h.x.timeline = [Post("1000", "old post", 0.0)]
    await h.send(ADMIN, "/feedon https://x.com/VeronaOfficial")
    feed = db.feeds_for_chat(chat_id)[0]
    assert feed["handle"] == "VeronaOfficial" and feed["since_id"] == "1000" and feed["mode"] == "posts"
    assert feed["pin"] == 0

    # New post with an image is forwarded; the reply is skipped in posts mode.
    h.x.timeline += [
        Post("1001", "gm Verona fam https://t.co/abc123", 0.0, "https://pbs.twimg.com/media/x.jpg", "post"),
        Post("1002", "@someone thanks!", 0.0, None, "reply"),
        Post("1003", "Big news <soon>", 0.0, None, "post"),
    ]
    tg.clear()
    await feed_job(h.ctx)
    photos = tg.of("sendPhoto")
    assert len(photos) == 1 and photos[0]["photo"] == "https://pbs.twimg.com/media/x.jpg"
    assert "gm Verona fam" in photos[0]["caption"] and "t.co" not in photos[0]["caption"]
    assert "https://x.com/VeronaOfficial/status/1001" in photos[0]["caption"]
    texts = tg.texts()
    assert len(texts) == 1 and "Big news &lt;soon&gt;" in texts[0] and "thanks" not in texts[0]
    assert tg.of("pinChatMessage") == []  # plain /feedon doesn't pin
    assert db.feeds_for_chat(chat_id)[0]["since_id"] == "1003"

    # Not due again yet; and once due, nothing is posted twice.
    calls = h.x.timeline_calls
    await feed_job(h.ctx)
    assert h.x.timeline_calls == calls
    db.conn.execute("UPDATE feeds SET last_checked_at = 0")
    db.conn.commit()
    tg.clear()
    await feed_job(h.ctx)
    assert tg.of("sendPhoto") == [] and tg.of("sendMessage") == []

    # 'all' mode includes replies.
    await h.send(ADMIN, "/feedon VeronaOfficial all")
    h.x.timeline.append(Post("1004", "@fan yes!", 0.0, None, "reply"))
    db.conn.execute("UPDATE feeds SET last_checked_at = 0")
    db.conn.commit()
    tg.clear()
    await feed_job(h.ctx)
    assert any("replied on X" in t and "@fan yes!" in t for t in tg.texts())

    # 'pin' pins every new post with a notification, and works together with 'all'.
    tg.clear()
    db.conn.execute("DELETE FROM scheduled_deletes")
    db.conn.commit()
    await h.send(ADMIN, "/feedon VeronaOfficial pin all")
    feed = db.feeds_for_chat(chat_id)[0]
    assert feed["pin"] == 1 and feed["mode"] == "all"
    assert any("pinned with a notification" in t for t in tg.texts())
    # The confirmation is only for the admin, so it deletes after 15 seconds like other confirmations.
    # Two deletes are scheduled: the /feedon command itself and the confirmation.
    waits = [r[0] - time.time() for r in db.conn.execute("SELECT delete_at FROM scheduled_deletes")]
    assert len(waits) == 2 and all(0 < w <= 15 for w in waits)
    h.x.timeline += [Post("1005", "Pin me", 0.0, None, "post"),
                     Post("1006", "Pin me too", 0.0, "https://pbs.twimg.com/media/y.jpg", "post")]
    db.conn.execute("UPDATE feeds SET last_checked_at = 0")
    db.conn.commit()
    tg.clear()
    await feed_job(h.ctx)
    sent_ids = [m for m, _ in tg.calls if m in ("sendMessage", "sendPhoto")]
    assert sent_ids == ["sendMessage", "sendPhoto"]
    pins = tg.of("pinChatMessage")
    assert len(pins) == 2
    assert all(str(p.get("disable_notification", False)).lower() in ("false", "0") for p in pins)

    # /feeds shows that it pins.
    tg.clear()
    await h.send(ADMIN, "/feeds")
    assert any("@VeronaOfficial (everything, pinned)" in t for t in tg.texts())

    # Running /feedon again without 'pin' turns pinning off.
    await h.send(ADMIN, "/feedon VeronaOfficial")
    assert db.feeds_for_chat(chat_id)[0]["pin"] == 0
    h.x.timeline.append(Post("1007", "No pin", 0.0, None, "post"))
    db.conn.execute("UPDATE feeds SET last_checked_at = 0")
    db.conn.commit()
    tg.clear()
    await feed_job(h.ctx)
    assert any("No pin" in t for t in tg.texts()) and tg.of("pinChatMessage") == []

    # Unknown or repeated options get the usage message.
    for bad in ("/feedon VeronaOfficial pinn", "/feedon VeronaOfficial pin pin"):
        tg.clear()
        await h.send(ADMIN, bad)
        assert any("Usage: /feedon" in t for t in tg.texts())

    # /feeds lists it, /feedoff stops it.
    tg.clear()
    await h.send(ADMIN, "/feeds")
    assert any("@VeronaOfficial" in t for t in tg.texts())
    await h.send(ADMIN, "/feedoff @VeronaOfficial")
    assert db.feeds_for_chat(chat_id) == []

    await h.app.shutdown()


def test_cost_savers(tmp_path):
    asyncio.run(_cost_savers(tmp_path))


async def _cost_savers(tmp_path):
    import raidbot.h_raid as h_raid
    h = Harness(tmp_path)
    tg, db = h.tg, h.db
    await h.start()
    chat_id = CHAT["id"]

    # Bob registered before the X key existed: no X account ID stored.
    await h.send(BOB, "hi")
    db.register_user(BOB["id"], "bobby", "Bob", "BobX", None, wallet(5))
    db.save_template(chat_id, "default", "Raid {xname} {link}", None)

    # Raid start: the free embed lookup gives the name, so no paid profile read.
    async def free_name(tweet_id, handle=None):
        return PostAuthor(name="Verona", username="VeronaOfficial")
    h_raid.oembed_author = free_name
    await h.send(ADMIN, "@raidall https://x.com/VeronaOfficial/status/1861234567890777777")
    assert getattr(h.x, "post_info_calls", 0) == 0
    assert any("Raid Verona https://x.com/VeronaOfficial/status/1861234567890777777" in t for t in tg.texts())
    raid = db.latest_raid(chat_id)
    assert raid["conversation_id"] == "1861234567890777777"

    # First check looks Bob's X ID up once, then matches his reply by ID alone.
    h.x.replies = [Reply("9100", "222", None, raid["started_at"] + 42)]
    await tracking_job(h.ctx)
    assert db.get_user(BOB["id"])["x_user_id"] == "222"
    assert db.engagements(raid["id"])[0]["reply_url"] == "https://x.com/BobX/status/9100"
    lookups = h.x.user_lookups
    db.update_raid(raid["id"], last_checked_at=0)
    await tracking_job(h.ctx)
    assert h.x.user_lookups == lookups  # not looked up again

    await h.app.shutdown()
