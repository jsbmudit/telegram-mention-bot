# Raid Bot: Final Spec

Status: agreed, ready to build. Hosting will be decided after the bot is built.

## Setup

- Written in Python, data stored in a local SQLite database.
- Secrets come only from environment variables, never from files in the repo:
  - `TELEGRAM_BOT_TOKEN` (required). If missing, the bot stops with "TELEGRAM_BOT_TOKEN is not set".
  - `X_BEARER_TOKEN` (optional until the X plan is confirmed). If missing, the bot runs normally but raid tracking is switched off and admins are told so.
- `.env` is in `.gitignore`. Tokens are never printed or logged.
- The bot must be a group admin with these rights: delete messages, pin messages, restrict members.
- Works in many groups at once. Templates, settings, raids and leaderboards are per group. Registration is per person, shared across groups.

## 1. Members

- `/addusers @a @b @c` (admin): adds the starting list of usernames.
- People are added automatically when they send a message or join, and removed when they leave.
- Each person is linked to their Telegram user ID once the bot sees them, so username changes don't break tags.
- People with no Telegram username can't be preloaded. They're added the first time they message.

## 2. Registration

- `/register` in the group: the bot replies with a Register button that opens a private chat with the bot.
- In the private chat, the bot asks two questions in a row:
  1. X profile: accepts `@name`, `name`, or an `x.com` / `twitter.com` link. Saved in one format. The bot looks up the X account ID so tracking survives handle changes.
  2. Verona address: must be a valid `xion1...` address. The checksum is checked, so typos get rejected.
- At the end it shows both answers with a Confirm button.
- One X account can belong to only one Telegram user.
- Running `/register` again updates the details.
- Wallet addresses are never posted in a group. They only appear in the admin CSV files.

## 3. `@all` (admins only)

- First it tags registered members in batches of 5.
- Then it tags unregistered members in batches of 5. Every one of those batches says: "UNREGISTERED USERS: register with /register to get tracked for raids."
- 15 minute cooldown per group, shared with `@raidall`.
- Tag messages delete themselves after 4 hours. Delete times are saved, so this still happens after a restart.

## 4. Raids

- `/settemplate` (admin): the bot asks for the template, the admin sends it. It must contain `{link}`, or the bot warns. Bold, italics and links are kept.
- `/template`: shows a preview.
- `@raidall` (admin):
  1. Uses the X link in the same message, or the latest X link the same admin posted.
  2. Tags everyone, the same way as `@all`.
  3. Posts the raid message with the link filled in.
  4. Pins it with notification. It is never unpinned.
- A raid lasts 12 hours, or until `/endraid`.
- More than one raid can be live at the same time. If several are live, `/endraid` shows buttons so the admin can pick which one to end.

## 5. Tracking (X API)

- While a raid is live, the bot checks the post's replies every 10 minutes. This can be changed in settings.
- Only registered members count. Replies from outsiders are ignored.
- Any reply in the post's thread counts, but only one per person. The time of their first reply is used.
- Each person's response time is saved, for example "2m 14s after the raid started".

## 6. Lock

- `/lock 15 60m` (admin): locks the chat for non-admins until 15 registered members have commented on the latest live raid, or until 60 minutes pass, whichever comes first.
- `/unlock` (admin): ends the lock early.
- Only works while a raid is live.
- The group's permissions are saved before locking and restored exactly afterwards.

## 7. Summaries

- Posted when a lock ends, whatever ended it, and when a raid ends.
- Contents: how many commented, the fastest commenters, and who missed it.

## 8. CSV reports (admins only, sent privately)

- `/raidcsv` gets the latest raid, `/raidcsv 3` gets a specific raid, and `/raidcsv all` gets every raid.
- The bot deletes the command from the group and sends the file to the admin in a private chat. Each admin presses Start on the bot once.
- Columns: Telegram username, X handle, Verona address, raided yes/no, reply link, reply time, time after raid start.

## 9. Leaderboard

- Scoring:
  - 1 point per raid joined
  - +1 bonus for commenting within 10 minutes
  - a streak counts consecutive raids joined
- Weeks run Monday to Sunday, UTC. The leaderboard is posted in the group automatically every Monday.
- `/leaderboard` shows the current week.
- Admins privately get a CSV of the top 5 with Verona addresses, for rewards.

## 10. Welcome message

- `/setwelcome` (admin): set the same way as the template. It includes a Register button.
- Posted automatically when someone joins.
- `/welcome` (admin) posts it on demand. When used as a reply to someone, it's linked to that person.
- It deletes itself once the person registers, or after 24 hours, whichever comes first. Telegram doesn't let bots delete messages older than 48 hours.
