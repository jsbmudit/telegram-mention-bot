"""SQLite storage. One connection, used from the bot's single event loop."""

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    chat_id       INTEGER PRIMARY KEY,
    title         TEXT,
    template_html TEXT,
    welcome_html  TEXT,
    last_tag_at   REAL NOT NULL DEFAULT 0,
    last_lb_week  TEXT
);

CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    x_handle      TEXT,
    x_user_id     TEXT,
    wallet        TEXT,
    registered_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS users_x_handle ON users(lower(x_handle)) WHERE x_handle IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS users_x_id ON users(x_user_id) WHERE x_user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS users_wallet ON users(wallet) WHERE wallet IS NOT NULL;

-- People in each group. Rows added with /addusers have a username but no
-- user_id until that person is seen, then the user_id is filled in.
CREATE TABLE IF NOT EXISTS members (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    user_id   INTEGER,
    username  TEXT,
    name      TEXT,
    added_at  REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS members_uid ON members(chat_id, user_id) WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS members_uname ON members(chat_id, lower(username)) WHERE user_id IS NULL;

CREATE TABLE IF NOT EXISTS recent_links (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    sender_id  INTEGER NOT NULL,
    tweet_id   TEXT NOT NULL,
    url        TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS recent_links_lookup ON recent_links(chat_id, sender_id, created_at);

CREATE TABLE IF NOT EXISTS raids (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    raid_no         INTEGER NOT NULL,
    tweet_id        TEXT NOT NULL,
    conversation_id TEXT,
    url             TEXT NOT NULL,
    started_by      INTEGER,
    started_at      REAL NOT NULL,
    ends_at         REAL NOT NULL,
    ended_at        REAL,
    end_reason      TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    message_id      INTEGER,
    since_id        TEXT,
    last_checked_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS raids_chat ON raids(chat_id, status);

CREATE TABLE IF NOT EXISTS engagements (
    raid_id        INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    reply_tweet_id TEXT NOT NULL,
    reply_url      TEXT NOT NULL,
    replied_at     REAL NOT NULL,
    PRIMARY KEY (raid_id, user_id)
);

CREATE TABLE IF NOT EXISTS locks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER NOT NULL,
    raid_id          INTEGER NOT NULL,
    target           INTEGER NOT NULL,
    started_at       REAL NOT NULL,
    ends_at          REAL NOT NULL,
    prev_permissions TEXT,
    active           INTEGER NOT NULL DEFAULT 1,
    end_reason       TEXT
);

CREATE TABLE IF NOT EXISTS scheduled_deletes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    delete_at   REAL NOT NULL,
    kind        TEXT NOT NULL,
    ref_user_id INTEGER
);
CREATE INDEX IF NOT EXISTS scheduled_deletes_due ON scheduled_deletes(delete_at);

-- Named raid templates per group. photo_file_id is set when the template has an image.
CREATE TABLE IF NOT EXISTS templates (
    chat_id       INTEGER NOT NULL,
    name          TEXT NOT NULL,
    body_html     TEXT NOT NULL,
    photo_file_id TEXT,
    updated_at    REAL NOT NULL,
    PRIMARY KEY (chat_id, name)
);

-- X accounts whose posts are forwarded into a group (/feedon, /feedoff).
CREATE TABLE IF NOT EXISTS feeds (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id         INTEGER NOT NULL,
    x_user_id       TEXT NOT NULL,
    handle          TEXT NOT NULL,
    name            TEXT,
    mode            TEXT NOT NULL DEFAULT 'posts',
    since_id        TEXT,
    last_checked_at REAL NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    UNIQUE (chat_id, x_user_id)
);
"""

DEFAULT_TEMPLATE = "default"

_GROUP_FIELDS = {"title", "template_html", "welcome_html", "last_tag_at", "last_lb_week"}
_RAID_FIELDS = {"conversation_id", "since_id", "last_checked_at", "message_id"}


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Bring databases made by older versions of the bot up to date."""
        lock_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(locks)")}
        if "message_id" not in lock_cols:
            self.conn.execute("ALTER TABLE locks ADD COLUMN message_id INTEGER")
        # Single templates from the first version become the "default" template.
        self.conn.execute(
            "INSERT OR IGNORE INTO templates(chat_id, name, body_html, photo_file_id, updated_at) "
            "SELECT chat_id, ?, template_html, NULL, ? FROM groups WHERE template_html IS NOT NULL",
            (DEFAULT_TEMPLATE, time.time()),
        )
        self.conn.execute("UPDATE groups SET template_html = NULL WHERE template_html IS NOT NULL")

    def close(self) -> None:
        self.conn.close()

    def _one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def _run(self, sql: str, params=()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    # ------------------------------------------------------------ groups

    def ensure_group(self, chat_id: int, title: str | None = None) -> None:
        self._run(
            "INSERT INTO groups(chat_id, title) VALUES(?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title = COALESCE(excluded.title, groups.title)",
            (chat_id, title),
        )

    def get_group(self, chat_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM groups WHERE chat_id = ?", (chat_id,))

    def all_groups(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM groups")

    def set_group(self, chat_id: int, field: str, value) -> None:
        if field not in _GROUP_FIELDS:
            raise ValueError(field)
        self.ensure_group(chat_id)
        self._run(f"UPDATE groups SET {field} = ? WHERE chat_id = ?", (value, chat_id))

    # ------------------------------------------------------------ templates

    def save_template(self, chat_id: int, name: str, body_html: str, photo_file_id: str | None) -> None:
        self.ensure_group(chat_id)
        self._run(
            "INSERT INTO templates(chat_id, name, body_html, photo_file_id, updated_at) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, name) DO UPDATE SET body_html = excluded.body_html, "
            "photo_file_id = excluded.photo_file_id, updated_at = excluded.updated_at",
            (chat_id, name.lower(), body_html, photo_file_id, time.time()),
        )

    def get_template(self, chat_id: int, name: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM templates WHERE chat_id = ? AND name = ?", (chat_id, name.lower()))

    def list_templates(self, chat_id: int) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM templates WHERE chat_id = ? ORDER BY name = ? DESC, name", (chat_id, DEFAULT_TEMPLATE)
        )

    def delete_template(self, chat_id: int, name: str) -> bool:
        cur = self._run("DELETE FROM templates WHERE chat_id = ? AND name = ?", (chat_id, name.lower()))
        return cur.rowcount == 1

    # ------------------------------------------------------------ feeds

    def add_feed(self, chat_id: int, x_user_id: str, handle: str, name: str | None,
                 mode: str, since_id: str | None) -> None:
        self.ensure_group(chat_id)
        self._run(
            "INSERT INTO feeds(chat_id, x_user_id, handle, name, mode, since_id, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(chat_id, x_user_id) DO UPDATE SET "
            "handle = excluded.handle, name = excluded.name, mode = excluded.mode, since_id = excluded.since_id",
            (chat_id, x_user_id, handle, name, mode, since_id, time.time()),
        )

    def remove_feed(self, chat_id: int, handle: str) -> bool:
        cur = self._run("DELETE FROM feeds WHERE chat_id = ? AND lower(handle) = lower(?)", (chat_id, handle))
        return cur.rowcount > 0

    def feeds_for_chat(self, chat_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM feeds WHERE chat_id = ? ORDER BY handle", (chat_id,))

    def all_feeds(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM feeds ORDER BY x_user_id")

    def update_feed(self, feed_id: int, since_id: str | None, checked_at: float) -> None:
        self._run("UPDATE feeds SET since_id = ?, last_checked_at = ? WHERE id = ?", (since_id, checked_at, feed_id))

    # ------------------------------------------------------------ users

    def upsert_user_profile(self, user_id: int, username: str | None, first_name: str | None) -> None:
        self._run(
            "INSERT INTO users(user_id, username, first_name) VALUES(?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, "
            "first_name = excluded.first_name",
            (user_id, username, first_name),
        )

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM users WHERE user_id = ?", (user_id,))

    def x_owner(self, x_handle: str, x_user_id: str | None) -> int | None:
        """The Telegram user who already registered this X account, if any."""
        row = self._one(
            "SELECT user_id FROM users WHERE lower(x_handle) = lower(?) "
            "OR (? IS NOT NULL AND x_user_id = ?)",
            (x_handle, x_user_id, x_user_id),
        )
        return row["user_id"] if row else None

    def wallet_owner(self, wallet: str) -> int | None:
        row = self._one("SELECT user_id FROM users WHERE wallet = ?", (wallet,))
        return row["user_id"] if row else None

    def register_user(self, user_id: int, username: str | None, first_name: str | None,
                      x_handle: str, x_user_id: str | None, wallet: str) -> None:
        self.upsert_user_profile(user_id, username, first_name)
        self._run(
            "UPDATE users SET x_handle = ?, x_user_id = ?, wallet = ?, "
            "registered_at = COALESCE(registered_at, ?) WHERE user_id = ?",
            (x_handle, x_user_id, wallet, time.time(), user_id),
        )

    def set_x_user_id(self, user_id: int, x_user_id: str) -> None:
        self._run("UPDATE users SET x_user_id = ? WHERE user_id = ?", (x_user_id, user_id))

    # ------------------------------------------------------------ members

    def upsert_member(self, chat_id: int, user_id: int, username: str | None, name: str | None) -> bool:
        """Record that this person is in the group. Returns True if they were new."""
        row = self._one("SELECT id FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        if row:
            self._run("UPDATE members SET username = ?, name = ? WHERE id = ?", (username, name, row["id"]))
            if username:
                # A /addusers row for the same name is now a duplicate.
                self._run(
                    "DELETE FROM members WHERE chat_id = ? AND user_id IS NULL AND lower(username) = lower(?)",
                    (chat_id, username),
                )
            return False
        if username:
            pending = self._one(
                "SELECT id FROM members WHERE chat_id = ? AND user_id IS NULL AND lower(username) = lower(?)",
                (chat_id, username),
            )
            if pending:
                self._run(
                    "UPDATE members SET user_id = ?, username = ?, name = ? WHERE id = ?",
                    (user_id, username, name, pending["id"]),
                )
                return False
        self._run(
            "INSERT INTO members(chat_id, user_id, username, name, added_at) VALUES(?, ?, ?, ?, ?)",
            (chat_id, user_id, username, name, time.time()),
        )
        return True

    def link_username_everywhere(self, user_id: int, username: str | None, name: str | None) -> None:
        """Attach a user_id to /addusers rows in every group that used this username."""
        if not username:
            return
        rows = self._all(
            "SELECT id, chat_id FROM members WHERE user_id IS NULL AND lower(username) = lower(?)",
            (username,),
        )
        for r in rows:
            exists = self._one(
                "SELECT 1 FROM members WHERE chat_id = ? AND user_id = ?", (r["chat_id"], user_id)
            )
            if exists:
                self._run("DELETE FROM members WHERE id = ?", (r["id"],))
            else:
                self._run(
                    "UPDATE members SET user_id = ?, username = ?, name = ? WHERE id = ?",
                    (user_id, username, name, r["id"]),
                )

    def add_usernames(self, chat_id: int, usernames: list[str]) -> tuple[int, int]:
        """Add /addusers names. Returns (added, already_there)."""
        added = existing = 0
        for name in usernames:
            have = self._one(
                "SELECT 1 FROM members WHERE chat_id = ? AND lower(username) = lower(?)", (chat_id, name)
            )
            if have:
                existing += 1
                continue
            known = self._one("SELECT user_id, first_name FROM users WHERE lower(username) = lower(?)", (name,))
            if known:
                if self.upsert_member(chat_id, known["user_id"], name, known["first_name"]):
                    added += 1
                else:
                    existing += 1
                continue
            self._run(
                "INSERT INTO members(chat_id, user_id, username, name, added_at) VALUES(?, NULL, ?, NULL, ?)",
                (chat_id, name, time.time()),
            )
            added += 1
        return added, existing

    def remove_member(self, chat_id: int, user_id: int) -> None:
        self._run("DELETE FROM members WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))

    def remove_member_by_username(self, chat_id: int, username: str) -> int:
        cur = self._run(
            "DELETE FROM members WHERE chat_id = ? AND lower(username) = lower(?)", (chat_id, username)
        )
        return cur.rowcount

    def list_members(self, chat_id: int) -> list[sqlite3.Row]:
        """Everyone in the group, with their registration details if they have any."""
        return self._all(
            """
            SELECT m.user_id,
                   COALESCE(m.username, u.username) AS username,
                   COALESCE(m.name, u.first_name)   AS name,
                   u.x_handle, u.x_user_id, u.wallet,
                   (u.registered_at IS NOT NULL)    AS registered
            FROM members m
            LEFT JOIN users u ON u.user_id = m.user_id
            WHERE m.chat_id = ?
            ORDER BY m.added_at, m.id
            """,
            (chat_id,),
        )

    # ------------------------------------------------------------ links

    def add_link(self, chat_id: int, sender_id: int, tweet_id: str, url: str) -> None:
        self._run(
            "INSERT INTO recent_links(chat_id, sender_id, tweet_id, url, created_at) VALUES(?, ?, ?, ?, ?)",
            (chat_id, sender_id, tweet_id, url, time.time()),
        )

    def latest_link(self, chat_id: int, sender_id: int, since: float) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM recent_links WHERE chat_id = ? AND sender_id = ? AND created_at >= ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (chat_id, sender_id, since),
        )

    def prune_links(self, before: float) -> None:
        self._run("DELETE FROM recent_links WHERE created_at < ?", (before,))

    # ------------------------------------------------------------ raids

    def create_raid(self, chat_id: int, tweet_id: str, url: str, started_by: int | None,
                    duration: int, message_id: int | None) -> sqlite3.Row:
        now = time.time()
        row = self._one("SELECT COALESCE(MAX(raid_no), 0) + 1 AS n FROM raids WHERE chat_id = ?", (chat_id,))
        cur = self._run(
            "INSERT INTO raids(chat_id, raid_no, tweet_id, url, started_by, started_at, ends_at, message_id) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, row["n"], tweet_id, url, started_by, now, now + duration, message_id),
        )
        return self.get_raid(cur.lastrowid)

    def get_raid(self, raid_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM raids WHERE id = ?", (raid_id,))

    def get_raid_by_no(self, chat_id: int, raid_no: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM raids WHERE chat_id = ? AND raid_no = ?", (chat_id, raid_no))

    def latest_raid(self, chat_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM raids WHERE chat_id = ? ORDER BY raid_no DESC LIMIT 1", (chat_id,))

    def list_raids(self, chat_id: int) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM raids WHERE chat_id = ? ORDER BY raid_no", (chat_id,))

    def active_raids(self, chat_id: int | None = None) -> list[sqlite3.Row]:
        if chat_id is None:
            return self._all("SELECT * FROM raids WHERE status = 'active' ORDER BY started_at")
        return self._all(
            "SELECT * FROM raids WHERE status = 'active' AND chat_id = ? ORDER BY started_at", (chat_id,)
        )

    def raids_between(self, chat_id: int, start: float, end: float) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM raids WHERE chat_id = ? AND started_at >= ? AND started_at < ? ORDER BY started_at",
            (chat_id, start, end),
        )

    def update_raid(self, raid_id: int, **fields) -> None:
        for key in fields:
            if key not in _RAID_FIELDS:
                raise ValueError(key)
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._run(f"UPDATE raids SET {sets} WHERE id = ?", (*fields.values(), raid_id))

    def finish_raid(self, raid_id: int, reason: str) -> bool:
        """Mark a raid ended. Returns False if it had already ended."""
        cur = self._run(
            "UPDATE raids SET status = 'ended', ended_at = ?, end_reason = ? WHERE id = ? AND status = 'active'",
            (time.time(), reason, raid_id),
        )
        return cur.rowcount == 1

    # ------------------------------------------------------------ engagements

    def add_engagement(self, raid_id: int, user_id: int, reply_tweet_id: str,
                       reply_url: str, replied_at: float) -> bool:
        """Save a member's reply. Keeps only their earliest one. True if new."""
        row = self._one(
            "SELECT replied_at FROM engagements WHERE raid_id = ? AND user_id = ?", (raid_id, user_id)
        )
        if row is None:
            self._run(
                "INSERT INTO engagements(raid_id, user_id, reply_tweet_id, reply_url, replied_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (raid_id, user_id, reply_tweet_id, reply_url, replied_at),
            )
            return True
        if replied_at < row["replied_at"]:
            self._run(
                "UPDATE engagements SET reply_tweet_id = ?, reply_url = ?, replied_at = ? "
                "WHERE raid_id = ? AND user_id = ?",
                (reply_tweet_id, reply_url, replied_at, raid_id, user_id),
            )
        return False

    def engagements(self, raid_id: int) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT e.*, u.username, u.first_name, u.x_handle, u.wallet
            FROM engagements e LEFT JOIN users u ON u.user_id = e.user_id
            WHERE e.raid_id = ? ORDER BY e.replied_at
            """,
            (raid_id,),
        )

    def engagement_count(self, raid_id: int) -> int:
        return self._one("SELECT COUNT(*) AS n FROM engagements WHERE raid_id = ?", (raid_id,))["n"]

    # ------------------------------------------------------------ locks

    def create_lock(self, chat_id: int, raid_id: int, target: int, duration: int,
                    prev_permissions: str | None) -> sqlite3.Row:
        now = time.time()
        cur = self._run(
            "INSERT INTO locks(chat_id, raid_id, target, started_at, ends_at, prev_permissions) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (chat_id, raid_id, target, now, now + duration, prev_permissions),
        )
        return self._one("SELECT * FROM locks WHERE id = ?", (cur.lastrowid,))

    def active_lock(self, chat_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM locks WHERE chat_id = ? AND active = 1 ORDER BY id DESC LIMIT 1", (chat_id,))

    def active_locks(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM locks WHERE active = 1")

    def set_lock_message(self, lock_id: int, message_id: int) -> None:
        self._run("UPDATE locks SET message_id = ? WHERE id = ?", (message_id, lock_id))

    def lock_for_raid(self, raid_id: int) -> sqlite3.Row | None:
        return self._one("SELECT * FROM locks WHERE raid_id = ? AND active = 1", (raid_id,))

    def finish_lock(self, lock_id: int, reason: str) -> bool:
        cur = self._run(
            "UPDATE locks SET active = 0, end_reason = ? WHERE id = ? AND active = 1", (reason, lock_id)
        )
        return cur.rowcount == 1

    # ------------------------------------------------------------ scheduled deletes

    def schedule_delete(self, chat_id: int, message_id: int, delete_at: float,
                        kind: str, ref_user_id: int | None = None) -> None:
        self._run(
            "INSERT INTO scheduled_deletes(chat_id, message_id, delete_at, kind, ref_user_id) VALUES(?, ?, ?, ?, ?)",
            (chat_id, message_id, delete_at, kind, ref_user_id),
        )

    def due_deletes(self, now: float) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM scheduled_deletes WHERE delete_at <= ? ORDER BY delete_at", (now,))

    def scheduled_for_user(self, kind: str, user_id: int, chat_id: int | None = None) -> list[sqlite3.Row]:
        """Pending deletes of one kind tied to a person, e.g. their welcome message."""
        if chat_id is None:
            return self._all(
                "SELECT * FROM scheduled_deletes WHERE kind = ? AND ref_user_id = ?", (kind, user_id)
            )
        return self._all(
            "SELECT * FROM scheduled_deletes WHERE kind = ? AND ref_user_id = ? AND chat_id = ?",
            (kind, user_id, chat_id),
        )

    def find_scheduled(self, chat_id: int, message_id: int, kind: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM scheduled_deletes WHERE chat_id = ? AND message_id = ? AND kind = ?",
            (chat_id, message_id, kind),
        )

    def remove_scheduled(self, row_id: int) -> None:
        self._run("DELETE FROM scheduled_deletes WHERE id = ?", (row_id,))
