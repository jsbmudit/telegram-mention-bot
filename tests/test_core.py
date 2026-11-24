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
    assert eng[2]["reply_url"] == "https://x.com/bobx/status/603"
    assert db.get_user(2)["x_user_id"] == "222"

    text = core.raid_summary_html(db, raid, "Done", tracking_on=True)
    assert "Commented: 2 of 2" in text and "alice, 1m 00s" in text

    data = core.raid_csv(db, CHAT, [raid]).decode("utf-8-sig")
    assert "AliceX" in data and wallet(1) in data and "1m 00s" in data


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
