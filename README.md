# Raid Bot

A Telegram bot for running X raids in a group. It tags everyone, posts and pins the raid message,
tracks who comments on X, locks the chat until enough people comment, and keeps a weekly leaderboard.

- What every command does: [COMMANDS.txt](COMMANDS.txt)
- The full agreed feature list: [SPEC.md](SPEC.md)

## Setup

1. Add the bot to your group and make it an **admin** with these rights:
   - Delete messages
   - Pin messages (for raid posts, and feeds turned on with `pin`)
   - Ban users (Telegram needs this right for /raidlock)

   Being an admin also lets the bot see every message. It needs that to keep the member list up to date
   and to find the X link for `@raidall`.
2. Every admin presses **Start** on the bot once in a private chat. The bot answers admin commands there, so
   the group only sees what the community needs. Admins who skip this get their answers in the group for a
   few seconds instead.
3. In the group, run `/addusers @name1 @name2 ...` once to load the starting member list.
4. Run `/settemplate` in the group, then send the raid message to the bot in the private chat it opens.

## Running it

The bot reads its secrets from environment variables only. Never put them in files that get committed.

| Variable | Required | What it is |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | The bot token from BotFather |
| `X_BEARER_TOKEN` | for tracking | X API bearer token. Without it, raids work but comments aren't tracked |
| `DB_PATH` | no | Where to keep the database (default `raidbot.db`) |
| `TRACK_INTERVAL_MINUTES` | no | How often to check replies during a raid (default 1) |
| `LOCK_CHECK_INTERVAL_MINUTES` | no | How often to check replies while the chat is locked (default 1) |
| `FEED_INTERVAL_MINUTES` | no | How often to check followed X accounts for new posts (default 1) |
| `BOT_OWNER_ID` | recommended | Your Telegram user ID. You get the backups and can run `/backup`. Without it, automatic backups are off |
| `BACKUP_INTERVAL_HOURS` | no | How often the automatic backup is sent (default 24, at most once an hour) |

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m raidbot
```

If `TELEGRAM_BOT_TOKEN` is missing, the bot stops with the message `TELEGRAM_BOT_TOKEN is not set`.

For local testing you can put the variables in a `.env` file (see `.env.example`). `.env` is in
`.gitignore`, so it never gets pushed.

The X API plan needs to include reading posts, user timelines and recent search. The Free plan can't read replies or timelines.

## Backups

Everything the bot knows is in one database file: registrations, member lists, templates, welcome messages,
feeds, raids and leaderboards. If that file is lost, all of it has to be set up again.

So the bot sends a copy of it to its owner on Telegram:

- **Automatically, once a day** (`BACKUP_INTERVAL_HOURS`). The copy lives in your Telegram, not on the server,
  so it survives even if the server or the hosting account is shut down.
- **On demand** with `/backup`. Send it in your private chat with the bot (or in a group: the command is removed
  and the file still comes to you privately). Handy before updating the bot.
- **Only to the owner** set in `BOT_OWNER_ID`. A backup holds every group's data, including X handles and Verona
  addresses, so it never goes to other admins or into a group.

To find your Telegram ID, send `/backup` to the bot before `BOT_OWNER_ID` is set. It replies with your ID.
Press Start on the bot once so it can message you. If it can't reach you, it tries again an hour later.

The copy is made with SQLite's own backup feature, so it's complete and consistent even while the bot is busy.

### Restoring a backup

1. Stop the bot.
2. Save the backup file at the path `DB_PATH` points to, for example `/opt/raidbot/data/raidbot.db`.
3. Delete any `raidbot.db-wal` and `raidbot.db-shm` files next to it. They belong to the old database, and SQLite
   could try to apply them to the restored one.
4. Start the bot. Registrations, members, templates, welcome messages, feeds, raids and leaderboards are all back.

## X API cost

X bills per post returned ($0.005 per post read at the time of writing), not per check. Each reply is
fetched once, so a raid costs about $0.005 times the number of replies the post gets. A `/register`
costs one profile read ($0.01). An X feed costs $0.005 per new post. Check the X Developer Console
after the first real raid to confirm.

Checking every minute doesn't cost more. A check that finds nothing new returns no posts, so it isn't
billed. It still shows up under Requests in the X Developer Console, which counts every call, billed or
not. X allows 450 reply searches and 10,000 timeline reads per 15 minutes. Each live raid and each feed
uses about 15 calls in that time, so the bot stays far below those limits. If a limit is ever hit, the
bot pauses its X checks until X allows them again.

## Tests

```bash
.venv/bin/pip install pytest
```

```bash
.venv/bin/python -m pytest -q
```

The tests run the real bot code against a fake Telegram and a fake X, so they don't touch any real group.

## Good to know

- Telegram doesn't let bots list a group's members. The bot builds its list from `/addusers`, people who
  send messages, and people who join.
- Telegram lets a bot send about 20 messages a minute in a group, so tagging 80 people (16 messages) takes
  about a minute.
- Telegram doesn't let bots delete messages older than 48 hours. Tags delete after 4 hours and welcome
  messages after 24 hours, so both stay within that limit.
- Secrets are never logged. Telegram request logs, which contain the token, are switched off, and every
  log line goes through a filter that hides tokens.
