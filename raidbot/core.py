"""Bot logic that only needs the database: tag lists, reply matching,
summaries, leaderboard and CSV files. Kept apart from Telegram so it can be tested."""

import csv
import io
import time
from datetime import timedelta

from .db import Database
from .utils import (
    chunk, csv_safe, format_duration, mention_html, plain_name, utc_str, week_start,
)
from .xapi import Reply

UNREGISTERED_LINE = "<b>UNREGISTERED USERS:</b> register with /register to get tracked for raids."
FAST_BONUS_SECONDS = 10 * 60


# ---------------------------------------------------------------- tagging

def build_tag_messages(db: Database, chat_id: int, batch_size: int = 5,
                       skip_user_ids: set[int] = frozenset()) -> list[str]:
    """Registered members first, then unregistered members with a reminder line."""
    registered, unregistered = [], []
    for m in db.list_members(chat_id):
        if m["user_id"] is not None and m["user_id"] in skip_user_ids:
            continue
        if m["user_id"] is None and not m["username"]:
            continue
        tag = mention_html(m["user_id"], m["username"], m["name"])
        (registered if m["registered"] else unregistered).append(tag)
    messages = [" ".join(batch) for batch in chunk(registered, batch_size)]
    messages += [UNREGISTERED_LINE + "\n" + " ".join(batch) for batch in chunk(unregistered, batch_size)]
    return messages


# ---------------------------------------------------------------- tracking

def record_replies(db: Database, raid, replies: list[Reply]) -> int:
    """Credit replies to registered members of the raid's group. Returns new count."""
    by_x_id, by_handle, handles = {}, {}, {}
    for m in db.list_members(raid["chat_id"]):
        if not m["registered"] or m["user_id"] is None:
            continue
        handles[m["user_id"]] = m["x_handle"]
        if m["x_user_id"]:
            by_x_id[m["x_user_id"]] = m["user_id"]
        if m["x_handle"]:
            by_handle[m["x_handle"].lower()] = (m["user_id"], m["x_user_id"])

    new = 0
    for r in replies:
        user_id = by_x_id.get(r.author_id)
        if user_id is None and r.author_username:
            match = by_handle.get(r.author_username.lower())
            if match and match[1] in (None, r.author_id):
                user_id = match[0]
                if match[1] is None:
                    # Registered before the X key was set: remember their X ID now.
                    db.set_x_user_id(user_id, r.author_id)
                    by_x_id[r.author_id] = user_id
        if user_id is None:
            continue  # not a registered member of this group
        handle = handles.get(user_id)
        url = f"https://x.com/{handle}/status/{r.tweet_id}" if handle else r.url
        if db.add_engagement(raid["id"], user_id, r.tweet_id, url, r.created_at):
            new += 1
    return new


# ---------------------------------------------------------------- summaries

def members_missing_x_id(db: Database, chat_id: int) -> list:
    """Registered members whose X account ID isn't known yet (they registered
    before the X key was set). Replies can only be matched by ID."""
    return [m for m in db.list_members(chat_id)
            if m["registered"] and m["user_id"] is not None and not m["x_user_id"] and m["x_handle"]]


def raid_summary_html(db: Database, raid, title: str, tracking_on: bool, max_names: int = 40) -> str:
    lines = [f"<b>{title}</b>", f"Raid #{raid['raid_no']}: {raid['url']}"]
    if not tracking_on:
        lines.append("Comment tracking is off because no X API key is set.")
        return "\n".join(lines)

    members = db.list_members(raid["chat_id"])
    registered = [m for m in members if m["registered"]]
    unregistered_count = len(members) - len(registered)
    engaged = db.engagements(raid["id"])
    engaged_ids = {e["user_id"] for e in engaged}

    lines.append(f"Commented: {len(engaged)} of {len(registered)} registered members")
    if engaged:
        lines.append("")
        lines.append("<b>Fastest</b>")
        for i, e in enumerate(engaged[:5], 1):
            took = format_duration(e["replied_at"] - raid["started_at"])
            lines.append(f"{i}. {plain_name(e['username'], e['first_name'])}, {took}")

    missed = [m for m in registered if m["user_id"] not in engaged_ids]
    if missed:
        names = [plain_name(m["username"], m["name"]) for m in missed]
        shown = ", ".join(names[:max_names])
        if len(names) > max_names:
            shown += f" and {len(names) - max_names} more"
        lines.append("")
        lines.append(f"<b>Missed ({len(missed)})</b>")
        lines.append(shown)
    if unregistered_count:
        lines.append("")
        lines.append(f"{unregistered_count} members aren't registered, so they can't be tracked.")
    return "\n".join(lines)


# ---------------------------------------------------------------- leaderboard

def leaderboard(db: Database, chat_id: int, start: float, end: float) -> list[dict]:
    """Points: 1 per raid joined, +1 if within 10 minutes. Streak: raids in a row."""
    week_raids = db.raids_between(chat_id, start, end)
    scores: dict[int, dict] = {}
    for raid in week_raids:
        for e in db.engagements(raid["id"]):
            s = scores.setdefault(e["user_id"], {
                "user_id": e["user_id"], "username": e["username"], "name": e["first_name"],
                "x_handle": e["x_handle"], "wallet": e["wallet"],
                "points": 0, "raids": 0, "fast": 0, "total_time": 0.0,
            })
            took = max(0.0, e["replied_at"] - raid["started_at"])
            s["raids"] += 1
            s["points"] += 1
            s["total_time"] += took
            if took <= FAST_BONUS_SECONDS:
                s["fast"] += 1
                s["points"] += 1

    # Current streak: raids joined in a row, newest first. A raid that is
    # still running only counts if they already joined it.
    all_raids = list(reversed(db.list_raids(chat_id)))
    joined = {r["id"]: {e["user_id"] for e in db.engagements(r["id"])} for r in all_raids}
    for s in scores.values():
        streak = 0
        for r in all_raids:
            if s["user_id"] in joined[r["id"]]:
                streak += 1
            elif r["status"] == "active":
                continue
            else:
                break
        s["streak"] = streak

    rows = list(scores.values())
    rows.sort(key=lambda s: (-s["points"], -s["streak"], s["total_time"] / max(1, s["raids"])))
    return rows


def leaderboard_html(rows: list[dict], start: float, title: str, limit: int = 10) -> str:
    ws = week_start(start)
    we = ws + timedelta(days=6)
    lines = [f"<b>{title}</b>", f"{ws:%b %d} to {we:%b %d} (UTC)", ""]
    if not rows:
        lines.append("No raid comments yet this week.")
        return "\n".join(lines)
    for i, s in enumerate(rows[:limit], 1):
        lines.append(
            f"{i}. {plain_name(s['username'], s['name'])}: {s['points']} pts, "
            f"{s['raids']} raids, streak {s['streak']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- CSV

RAID_CSV_HEADER = [
    "raid_no", "raid_link", "raid_started_utc", "telegram_username", "telegram_name",
    "x_handle", "verona_address", "registered", "raided", "reply_link",
    "reply_time_utc", "time_after_start",
]


def raid_csv(db: Database, chat_id: int, raids) -> bytes:
    members = db.list_members(chat_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(RAID_CSV_HEADER)
    for raid in raids:
        engaged = {e["user_id"]: e for e in db.engagements(raid["id"])}
        seen = set()
        people = []
        for m in members:
            people.append((m["user_id"], m["username"], m["name"], m["x_handle"], m["wallet"], m["registered"]))
            seen.add(m["user_id"])
        for uid, e in engaged.items():  # commented but has since left the group
            if uid not in seen:
                people.append((uid, e["username"], e["first_name"], e["x_handle"], e["wallet"], 1))
        for uid, username, name, x_handle, wallet, registered in people:
            e = engaged.get(uid) if uid is not None else None
            w.writerow([csv_safe(v) for v in (
                raid["raid_no"], raid["url"], utc_str(raid["started_at"]),
                username or "", name or "", x_handle or "", wallet or "",
                "yes" if registered else "no",
                "yes" if e else "no",
                e["reply_url"] if e else "",
                utc_str(e["replied_at"]) if e else "",
                format_duration(e["replied_at"] - raid["started_at"]) if e else "",
            )])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel reads it as UTF-8


def top_csv(rows: list[dict], limit: int = 5) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rank", "telegram_username", "telegram_name", "x_handle", "verona_address",
                "points", "raids_joined", "fast_bonuses", "streak"])
    for i, s in enumerate(rows[:limit], 1):
        w.writerow([csv_safe(v) for v in (
            i, s["username"] or "", s["name"] or "", s["x_handle"] or "", s["wallet"] or "",
            s["points"], s["raids"], s["fast"], s["streak"],
        )])
    return buf.getvalue().encode("utf-8-sig")


def now() -> float:
    return time.time()
