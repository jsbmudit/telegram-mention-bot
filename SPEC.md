# Raid Bot: Final Spec

Status: built. Hosting will be decided next. Every command is explained in COMMANDS.txt.

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
- `/removeuser @name` (admin): takes someone off the tag list.
- `/members` (admin): shows who is and isn't registered.

## 2. Registration

- `/register` in the group: the bot replies with a Register button that opens a private chat with the bot.
- In the private chat, the bot asks two questions in a row:
  1. X profile: accepts `@name`, `name`, or an `x.com` / `twitter.com` link. Saved in one format. The bot looks up the X account ID so tracking survives handle changes.
  2. Verona address: must be a valid `xion1...` address. The checksum is checked, so typos get rejected.
- At the end it shows both answers with a Confirm button.
- One X account and one Verona address can each belong to only one Telegram user.
- `/me` in the private chat shows what you registered. `/cancel` stops registration halfway.
- Running `/register` again updates the details.
- Wallet addresses are never posted in a group. They only appear in the admin CSV files.

## 3. `@all` (admins only)

- First it tags registered members in batches of 5.
- Then it tags unregistered members in batches of 5. Every one of those batches says: "UNREGISTERED USERS: register with /register to get tracked for raids."
- 15 minute cooldown per group, shared with `@raidall`.
- `/all` works the same. Editing an old message that says @all does not tag again.
- Tag messages delete themselves after 4 hours. Delete times are saved, so this still happens after a restart.

## 4. Raids

- Named templates per group: `/settemplate` saves the default one, `/settemplate fun` saves one called fun.
  The admin sends text, or a photo with the template as its caption to attach that image. It must contain `{link}`.
  Bold, italics and links are kept.
- Template variables: `{link}`, `{xname}` (X display name of the post author), `{xhandle}`, `{xprofile}`.
  The author is looked up with the X API when a key is set, otherwise with X's public embed service.
- `/templates` lists them, `/template fun` previews one, `/deltemplate fun` deletes one.
- `@raidall` (admin, also `@allraid`, `/raidall`, `/allraid`), optionally followed by a template name:
  1. Uses the X link in the same message, or the latest X link the same admin posted.
  2. Tags everyone, the same way as `@all`.
  3. Posts the chosen template with the link and author filled in, with its image if it has one.
  4. Pins it with notification. It is never unpinned.
- A raid lasts 12 hours, or until `/endraid`.
- More than one raid can be live at the same time. If several are live, `/endraid` shows buttons so the admin can pick which one to end.

## 4b. Keeping the chat clean

- Bot commands are deleted from the group about 3 seconds after they're sent.
- `@all` / `@raidall name <link>` sent on their own are deleted too. A real message around them is left alone.
- Short answers delete after 15 seconds; previews, lists, help and on-demand leaderboards after 1 minute.
- The /register button deletes as soon as someone opens the bot through it, or after 15 seconds.
- Setup prompts and the admin's template message are deleted once a template is saved.
- The lock notice is removed when the chat unlocks.
- Kept: the pinned raid message, summaries, and the weekly leaderboard.

## 5. Tracking (X API)

- While a raid is live, the bot checks the post's replies every 10 minutes. While the chat is locked, it checks every 2 minutes so the chat opens soon after the target is hit. Both can be changed with environment variables.
- Only registered members count. Replies from outsiders are ignored.
- Any reply in the post's thread counts, but only one per person. The time of their first reply is used.
- Each person's response time is saved, for example "2m 14s after the raid started".

## 6. Lock

- `/raidlock 15 60m` (admin): locks the chat for non-admins until 15 registered members have commented on the latest live raid, or until 60 minutes pass, whichever comes first.
- `/raidunlock` (admin): ends the lock early.
- Only works while a raid is live.
- The group's permissions are saved before locking and restored exactly afterwards.

## 7. Summaries

- Posted when a lock ends, whatever ended it, and when a raid ends.
- Contents: how many commented, the fastest commenters, and who missed it.
- Names in summaries and leaderboards are written without @, so they don't ping people again.

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

## 10. X feed

- `/feedon VeronaOfficial` (admin): posts that account's new X posts into the group. `all` at the end also includes replies and reposts.
- `/feedoff VeronaOfficial` stops it, `/feeds` lists what's on. Any number of accounts and groups.
- Starts from the newest post, checks every 5 minutes (`FEED_INTERVAL_MINUTES`), sends images when a post has one, and caps at 10 posts per check so a long outage doesn't flood the chat.
- One X request per account per check, even when several groups follow the same account.
- Needs the X API key.

## 11. Command names

- Chosen so they don't clash with Rose: `/raidlock`, `/raidunlock`, `/greet`, `/setgreet`, `/raidhelp`.

## 12. Welcome message

- `/setgreet` (admin): set the same way as the template. It includes a Register button.
- Posted automatically when someone joins.
- `/greet` (admin) posts it on demand. When used as a reply to someone, it's linked to that person.
- It deletes itself once the person registers, or after 24 hours, whichever comes first. Telegram doesn't let bots delete messages older than 48 hours.
