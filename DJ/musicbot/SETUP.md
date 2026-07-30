# Music Bot — Setup Guide (macOS)

Text a YouTube link (or a song name) to a private Telegram bot and it downloads
the MP3 on your Mac using your existing `music-dl` script. The bot uses
**long polling** — it only dials *out* to Telegram, so you never open a port or
expose your Mac to the internet.

You'll run the commands below yourself in **Terminal** (I can't type into your
Terminal). Copy-paste them one block at a time. Anything in ALL-CAPS like
`YOURNAME` is a placeholder you replace.

**Files in this bundle:**

- `music_bot.py` — the bot
- `com.user.musicbot.plist` — the launchd config that keeps it running
- `requirements.txt` — the one Python dependency
- `SETUP.md` — this guide

Throughout, I'll assume you put everything in `~/musicbot`. Adjust if you prefer
another folder.

---

## 1. Create the bot with @BotFather and copy the token

1. Open Telegram (phone or desktop) and search for **@BotFather** (the one with
   the blue checkmark).
2. Send `/newbot`.
3. Give it a **name** (e.g. "My Music Downloader") and a **username** ending in
   `bot` (e.g. `my_music_dl_bot` — must be unique).
4. BotFather replies with a **token** that looks like
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. **Copy it** — you'll paste
   it into the plist in step 5. Treat it like a password.

---

## 2. Find your numeric Telegram user ID

The bot only obeys people on an allowlist of numeric user IDs. Get yours by
**either** method:

- **Easiest:** in Telegram, message **@userinfobot** — it replies with your ID.
- **Or:** finish the setup, start a chat with *your* bot, and send `/id`. The
  bot replies with your ID even before you're on the allowlist (that command is
  open on purpose so you can bootstrap).

Write your ID down (a number like `111111111`). If more than one person should
use the bot, collect each person's ID.

---

## 3. Put the files in place and install the Python dependency

Open **Terminal** and run:

```bash
# Make a home for the bot and copy in the files.
mkdir -p ~/musicbot
# Copy the four files from wherever you saved them into ~/musicbot, e.g.:
#   cp ~/Downloads/music_bot.py ~/Downloads/requirements.txt \
#      ~/Downloads/com.user.musicbot.plist ~/Downloads/SETUP.md ~/musicbot/
cd ~/musicbot

# Create a dedicated virtual environment (keeps deps isolated and avoids the
# "externally-managed-environment" / pip errors you get installing globally).
python3 -m venv venv

# Install the bot's dependency into the venv.
./venv/bin/python3 -m pip install --upgrade pip
./venv/bin/python3 -m pip install -r requirements.txt
```

Confirm the download tools your `music-dl` needs are installed and on PATH:

```bash
# If you don't have Homebrew yet, install it from https://brew.sh first.
brew install yt-dlp ffmpeg

# Verify they're found:
which yt-dlp ffmpeg
```

Make sure `music-dl` itself is executable:

```bash
chmod +x /Users/orlandocedeno/DJ/musicbot   # <-- use your real path
```

---

## 4. Edit the plist placeholders

Open the plist in a text editor:

```bash
open -e ~/musicbot/com.user.musicbot.plist
```

Replace every **EDIT-ME** value. In particular:

| Placeholder | Replace with |
|---|---|
| `/Users/YOURNAME/musicbot/venv/bin/python3` | your venv python (run `echo ~/musicbot/venv/bin/python3` to get the full path — no `~`) |
| `/Users/YOURNAME/musicbot/music_bot.py` | full path to `music_bot.py` |
| `TELEGRAM_BOT_TOKEN` value | your BotFather token from step 1 |
| `ALLOWED_USER_IDS` value | your ID(s) from step 2, comma-separated, e.g. `111111111,222222222` |
| `MUSIC_DL` value | full path to your `music-dl` script |
| `MUSIC_DIR` value | the `Music` folder next to `music-dl` (or delete the key) |
| `WorkingDirectory` | the folder containing `music-dl` |
| both log paths | replace `YOURNAME` with your username (`whoami`) |

**Important:** launchd needs **absolute** paths — no `~` and no `$HOME`. Run
`whoami` to get your username and build full `/Users/…` paths.

The `PATH` line already covers Apple-Silicon (`/opt/homebrew/bin`) and Intel
(`/usr/local/bin`) Homebrew, so `yt-dlp`/`ffmpeg` will be found even though
launchd starts with a minimal environment.

---

## 5. Install and load the LaunchAgent

```bash
# Copy the plist into the LaunchAgents folder (create it if missing).
mkdir -p ~/Library/LaunchAgents
cp ~/musicbot/com.user.musicbot.plist ~/Library/LaunchAgents/

# Load it (starts the bot now and on every login).
launchctl load ~/Library/LaunchAgents/com.user.musicbot.plist
```

Check it's running and watch the log:

```bash
# Should list the job (a PID in the first column means it's alive):
launchctl list | grep musicbot

# Tail the log (Ctrl-C to stop watching). You should see
# "Starting music bot. Authorized user IDs: [...]".
tail -f ~/Library/Logs/musicbot.log
```

If the job keeps restarting, the log will show the reason (e.g. bad token, a
path typo, or a missing dependency).

---

## 6. Keep the Mac awake so downloads don't stall

If the Mac sleeps, the bot pauses and links you send won't process until it
wakes. Options:

- **Simplest, per-session:** run `caffeinate -s` in a Terminal window and leave
  it open — the Mac won't sleep while it runs. (`caffeinate -i` prevents idle
  sleep without needing the window focused.)
- **Permanent:** System Settings → **Displays** (or **Battery** → Options,
  depending on macOS) → set **"Prevent automatic sleeping"** / turn off sleep
  when plugged in.

---

## 7. Test it

1. On your phone, open Telegram and start a chat with your bot (tap its
   username, then **Start**).
2. Send `/id` — it should reply with your user ID. Confirm that ID is in
   `ALLOWED_USER_IDS` in the plist. (If you just added it, reload — see step 8.)
3. Send `/help` — it should reply with usage.
4. Send a YouTube link, e.g. a music video URL. You should see
   **"⬇️ Downloading…"** then **"✅ Done! Saved: <filename>.mp3"**.
5. Confirm the MP3 appeared in your `Music` folder:
   ```bash
   ls -lt /Users/YOURNAME/music/Music | head
   ```
6. Try a plain song name (e.g. `daft punk one more time`) — the bot passes it to
   `music-dl`, which searches YouTube. Send the same link twice to see the
   **"Already in your library — skipped"** reply (thanks to the download
   archive).

---

## 8. Updating, unloading, reloading

After editing the plist **or** `music_bot.py`, restart the bot so changes take
effect:

```bash
# If you edited the plist, copy the new version over first:
cp ~/musicbot/com.user.musicbot.plist ~/Library/LaunchAgents/

# Unload then load (works on all macOS versions):
launchctl unload ~/Library/LaunchAgents/com.user.musicbot.plist
launchctl load   ~/Library/LaunchAgents/com.user.musicbot.plist
```

To **stop** the bot entirely (won't restart, won't run at next login):

```bash
launchctl unload ~/Library/LaunchAgents/com.user.musicbot.plist
```

To **update the Python dependency** later:

```bash
~/musicbot/venv/bin/python3 -m pip install --upgrade -r ~/musicbot/requirements.txt
# then unload/load as above
```

---

## Troubleshooting

- **Bot doesn't reply at all** → check `launchctl list | grep musicbot` and the
  log at `~/Library/Logs/musicbot.log`. A missing/invalid token is the usual
  cause.
- **"Sorry — you're not authorized"** → your ID isn't in `ALLOWED_USER_IDS`.
  Send `/id`, add that number to the plist, copy it over, and reload (step 8).
- **"Could not run music-dl" / exit errors** → verify the `MUSIC_DL` path is
  correct and executable (`chmod +x`), and that `yt-dlp` and `ffmpeg` are on the
  `PATH` line in the plist.
- **Downloads stall or never finish** → the Mac is probably sleeping; see
  step 6. Very long items may hit the 30-minute subprocess timeout (adjustable
  via the `MUSIC_DL_TIMEOUT` env var, in seconds).
- **`launchctl load` says "already loaded"** → unload first (step 8), then load.

---

### Security notes

- The allowlist is enforced on **every** message and command (except `/id`,
  which is intentionally open so you can find your own ID). Anyone not on the
  list is refused and never reaches `music-dl`.
- The bot passes your message to `music-dl` as a **single argument** via a
  no-shell subprocess call, so there's no shell-injection surface from message
  text.
- Keep your BotFather token private. If it ever leaks, message @BotFather and
  use `/revoke` to issue a new one, then update the plist and reload.
