from raidbot import core
from raidbot.db import Database
from raidbot.utils import bech32_encode, convert_bits
from raidbot.xapi import Reply

CHAT = -100123


def wallet(n: int) -> str:
    return bech32_encode("xion", convert_bits([n] * 20, 8, 5, True))


def setup_db(tmp_path) -> Database:
    db = Database(str(tmp_path / "t.db"))
    db.ensure_group(CHAT, "Test")
    return db


def test_members_preload_then_link(tmp_path):
    db = setup_db(tmp_path)
    added, existing = db.add_usernames(CHAT, ["alice", "bob", "Alice"])
    assert (added, existing) == (2, 1)
    # alice speaks: the preloaded row gets her user_id, no duplicate.
    assert db.upsert_member(CHAT, 1, "alice", "Alice") is False
    rows = db.list_members(CHAT)
    assert len(rows) == 2
    assert {r["user_id"] for r in rows} == {1, None}
    # Someone with no username joins.
    assert db.upsert_member(CHAT, 3, None, "Carol") is True
    assert len(db.list_members(CHAT)) == 3
    db.remove_member(CHAT, 3)
    assert len(db.list_members(CHAT)) == 2


def test_tag_messages_registered_first(tmp_path):
    db = setup_db(tmp_path)
    for i in range(1, 9):
        db.upsert_user_profile(i, f"user{i}", f"U{i}")
        db.upsert_member(CHAT, i, f"user{i}", f"U{i}")
    for i in (2, 4, 6, 7, 8, 1):
        db.register_user(i, f"user{i}", f"U{i}", f"x{i}", str(1000 + i), wallet(i))
    db.upsert_member(CHAT, 99, None, "NoName")
    msgs = core.build_tag_messages(db, CHAT, 5)
    assert len(msgs) == 3
    assert "UNREGISTERED" not in msgs[0] and "UNREGISTERED" not in msgs[1]
    assert msgs[0].count("@") == 5 and msgs[1].count("@") == 1
    assert msgs[2].startswith(core.UNREGISTERED_LINE)
    assert "@user3" in msgs[2] and "@user5" in msgs[2] and 'tg://user?id=99' in msgs[2]


def test_record_replies_only_registered_group_members(tmp_path):
    db = setup_db(tmp_path)
    db.upsert_user_profile(1, "alice", "Alice")
    db.upsert_member(CHAT, 1, "alice", "Alice")
    db.register_user(1, "alice", "Alice", "AliceX", "111", wallet(1))
    db.upsert_user_profile(2, "bob", "Bob")
    db.upsert_member(CHAT, 2, "bob", "Bob")
    db.register_user(2, "bob", "Bob", "BobX", None, wallet(2))  # registered without X ID
    raid = db.create_raid(CHAT, "500", "https://x.com/p/status/500", 1, 12 * 3600, 10)
    t0 = raid["started_at"]
    replies = [
        Reply("601", "111", "AliceX", t0 + 120),
        Reply("602", "111", "AliceX", t0 + 60),     # earlier reply wins
        Reply("603", "222", "bobx", t0 + 900),      # matched by handle, X ID saved
        Reply("604", "999", "Outsider", t0 + 30),   # not in the group: ignored
    ]
    assert core.record_replies(db, raid, replies) == 2
    eng = {e["user_id"]: e for e in db.engagements(raid["id"])}
    assert eng[1]["reply_tweet_id"] == "602"
    assert eng[2]["reply_url"] == "https://x.com/BobX/status/603"  # built from the registered handle
    assert db.get_user(2)["x_user_id"] == "222"

    text = core.raid_summary_html(db, raid, "Done", tracking_on=True)
    assert "Commented: 2 of 2" in text and "alice, 1m 00s" in text

    data = core.raid_csv(db, CHAT, [raid]).decode("utf-8-sig")
    assert "https://x.com/AliceX" in data and wallet(1) in data and "1m 00s" in data


def test_raid_csv_layout(tmp_path):
    import csv
    import io
    db = setup_db(tmp_path)
    db.upsert_user_profile(1, "alice", "Alice")
    db.upsert_member(CHAT, 1, "alice", "Alice")
    db.register_user(1, "alice", "Alice", "AliceX", "111", wallet(1))
    db.upsert_member(CHAT, 2, None, "Carol")  # no Telegram username, not registered
    r1 = db.create_raid(CHAT, "501", "https://x.com/p/status/501", 1, 3600, 10)
    r2 = db.create_raid(CHAT, "502", "https://x.com/p/status/502", 1, 3600, 11)
    db.add_engagement(r1["id"], 1, "https://x.com/AliceX/status/9", "alice", r1["started_at"] + 90)

    rows = list(csv.reader(io.StringIO(core.raid_csv(db, CHAT, [r1]).decode("utf-8-sig"))))
    # Raid details appear once at the top, not on every row.
    assert rows[0] == ["raid_no", str(r1["raid_no"])]
    assert rows[1] == ["raid_link", "https://x.com/p/status/501"]
    assert rows[2][0] == "raid_started_utc" and rows[2][1].endswith("UTC")
    assert rows[3] == []
    assert rows[4] == core.RAID_CSV_HEADER
    assert "telegram_name" not in rows[4] and "x_handle" not in rows[4] and "x_profile" in rows[4]
    people = {r[0]: r for r in rows[5:]}
    assert people["alice"][1] == "https://x.com/AliceX" and people["alice"][4] == "yes"
    assert people["alice"][7] == "1m 30s"
    assert people["Carol"][1] == "" and people["Carol"][3] == "no"  # display name when no username
    assert len(rows) == 7

    # /raidcsv all: one block per raid, separated by a blank row.
    text = core.raid_csv(db, CHAT, [r1, r2]).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert [r for r in rows if r and r[0] == "raid_no"] == [["raid_no", str(r1["raid_no"])],
                                                            ["raid_no", str(r2["raid_no"])]]
    assert rows[7] == [] and rows[8] == ["raid_no", str(r2["raid_no"])]
    assert text.count("https://x.com/p/status/50") == 2


def test_leaderboard_points_and_streaks(tmp_path):
    db = setup_db(tmp_path)
    for i in (1, 2):
        db.upsert_user_profile(i, f"u{i}", f"U{i}")
        db.upsert_member(CHAT, i, f"u{i}", f"U{i}")
        db.register_user(i, f"u{i}", f"U{i}", f"x{i}", str(i), wallet(i))
    raids = [db.create_raid(CHAT, str(700 + n), f"https://x.com/p/status/{700 + n}", 1, 3600, n) for n in range(3)]
    for r in raids:
        db.finish_raid(r["id"], "test")
    # u1 joins all three fast; u2 joins raid 1 slowly and raid 3 fast.
    for r in raids:
        db.add_engagement(r["id"], 1, f"a{r['id']}", "u", r["started_at"] + 60)
    db.add_engagement(raids[0]["id"], 2, "b1", "u", raids[0]["started_at"] + 3000)
    db.add_engagement(raids[2]["id"], 2, "b3", "u", raids[2]["started_at"] + 30)
    rows = core.leaderboard(db, CHAT, 0, 4e9)
    assert [r["user_id"] for r in rows] == [1, 2]
    assert rows[0]["points"] == 6 and rows[0]["streak"] == 3
    assert rows[1]["points"] == 3 and rows[1]["streak"] == 1
    top = core.top_csv(rows).decode("utf-8-sig")
    assert wallet(1) in top and top.count("\n") == 3
    assert top.startswith("rank,telegram_username,x_profile,verona_address,")
    assert "https://x.com/x1" in top


def test_old_single_template_becomes_default(tmp_path):
    path = str(tmp_path / "old.db")
    db = Database(path)
    db.ensure_group(CHAT, "Test")
    db.conn.execute("UPDATE groups SET template_html = 'Old raid {link}' WHERE chat_id = ?", (CHAT,))
    db.conn.commit()
    db.close()
    db = Database(path)  # reopening runs the upgrade
    assert db.get_template(CHAT, "default")["body_html"] == "Old raid {link}"
    assert db.get_group(CHAT)["template_html"] is None


def test_replies_matched_by_x_id_without_author_profiles(tmp_path):
    db = setup_db(tmp_path)
    db.upsert_user_profile(1, "alice", "Alice")
    db.upsert_member(CHAT, 1, "alice", "Alice")
    db.register_user(1, "alice", "Alice", "AliceX", "111", wallet(1))
    raid = db.create_raid(CHAT, "500", "https://x.com/p/status/500", 1, 3600, 10)
    replies = [Reply("701", "111", None, raid["started_at"] + 5), Reply("702", "999", None, raid["started_at"] + 6)]
    assert core.record_replies(db, raid, replies) == 1
    assert db.engagements(raid["id"])[0]["reply_url"] == "https://x.com/AliceX/status/701"


def test_feeds_get_pin_column_on_old_databases(tmp_path):
    import sqlite3
    path = str(tmp_path / "old_feeds.db")
    db = Database(path)
    db.close()
    conn = sqlite3.connect(path)  # make it look like a database from before 'pin' existed
    conn.execute("ALTER TABLE feeds DROP COLUMN pin")
    conn.execute("INSERT INTO feeds(chat_id, x_user_id, handle, mode, created_at) VALUES(?, '1', 'old', 'posts', 0)",
                 (CHAT,))
    conn.commit()
    conn.close()
    db = Database(path)  # reopening runs the upgrade
    assert db.feeds_for_chat(CHAT)[0]["pin"] == 0
    db.add_feed(CHAT, "1", "old", None, "posts", None, pin=True)
    assert db.feeds_for_chat(CHAT)[0]["pin"] == 1
