# Raid Bot

A Telegram bot for running X raids in a group. It tags everyone, posts and pins the raid message,
tracks who comments on X, locks the chat until enough people comment, and keeps a weekly leaderboard.

- What every command does: [COMMANDS.txt](COMMANDS.txt)
- The full agreed feature list: [SPEC.md](SPEC.md)

## Setup

1. Add the bot to your group and make it an **admin** with these rights:
   - Delete messages
   - Pin messages
   - Ban users (Telegram needs this right for /lock)

   Being an admin also lets the bot see every message. It needs that to keep the member list up to date
   and to find the X link for `@raidall`.
2. Every admin presses **Start** on the bot once in a private chat, so it can send them reports.
3. In the group, run `/addusers @name1 @name2 ...` once to load the starting member list.
4. Run `/settemplate` to set the raid message.

## Running it

The bot reads its secrets from environment variables only. Never put them in files that get committed.

| Variable | Required | What it is |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | yes | The bot token from BotFather |
| `X_BEARER_TOKEN` | for tracking | X API bearer token. Without it, raids work but comments aren't tracked |
| `DB_PATH` | no | Where to keep the database (default `raidbot.db`) |
| `TRACK_INTERVAL_MINUTES` | no | How often to check replies during a raid (default 10) |
| `LOCK_CHECK_INTERVAL_MINUTES` | no | How often to check replies while the chat is locked (default 2) |

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

The X API plan needs to include reading posts and recent search. The Free plan can't read replies.

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
