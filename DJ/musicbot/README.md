# Music Download Bot — Operations Reference

A Telegram bot that downloads music to this Mac when you text it a link. Paste a
**YouTube**, **SoundCloud**, **Spotify**, or **Shazam** link (or a song name)
and it saves an MP3 into your Music folder by calling the `music-dl` script.

It runs as a launchd LaunchAgent (auto-starts at login, restarts if it crashes)
and talks to Telegram over long polling — outbound only, so no open ports.

All commands below are run in **Terminal on this Mac**. Paths assume the repo is
at `/Users/orlandocedeno/Music-Download-Bot`.

---

## Layout

```
Music-Download-Bot/
└── DJ/
    ├── music-dl               # the yt-dlp/ffmpeg download script
    ├── Music/                 # downloaded MP3s land here (gitignored)
    └── musicbot/
        ├── music_bot.py       # the bot
        ├── com.user.musicbot.plist
        ├── requirements.txt   # python-telegram-bot + spotdl
        ├── .env               # YOUR secrets/config (gitignored — never commit)
        ├── .env.example       # template
        └── venv/              # python virtualenv (gitignored)
```

## Configuration (.env)

Secrets and paths live in `DJ/musicbot/.env`, which is gitignored. The bot loads
it at startup; the plist only sets `PATH`. Keys:

```
TELEGRAM_BOT_TOKEN=...      # from @BotFather
ALLOWED_USER_IDS=...        # comma-separated numeric Telegram IDs
MUSIC_DL=/Users/orlandocedeno/Music-Download-Bot/DJ/music-dl
MUSIC_DIR=/Users/orlandocedeno/Music-Download-Bot/DJ/Music
SPOTDL=/Users/orlandocedeno/Music-Download-Bot/DJ/musicbot/venv/bin/spotdl
```

Create it from the template if needed: `cp .env.example .env` then edit.
After editing `.env`, reload the bot (see below).

---

## One-time install

```bash
cd /Users/orlandocedeno/Music-Download-Bot/DJ/musicbot

# Python deps into the venv (python-telegram-bot + spotdl for Spotify)
./venv/bin/python3 -m pip install --upgrade pip
./venv/bin/python3 -m pip install -r requirements.txt

# Download tools that music-dl / spotdl rely on
brew install yt-dlp ffmpeg
```

Install the LaunchAgent:

```bash
cp /Users/orlandocedeno/Music-Download-Bot/DJ/musicbot/com.user.musicbot.plist \
   ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.user.musicbot.plist
```

---

## Load / unload

```bash
# Load — start now and on every login
launchctl load ~/Library/LaunchAgents/com.user.musicbot.plist

# Unload — stop and prevent restart
launchctl unload ~/Library/LaunchAgents/com.user.musicbot.plist
```

## Reload (after editing plist, .env, or music_bot.py)

```bash
# If you changed the plist, copy the new one in first:
cp /Users/orlandocedeno/Music-Download-Bot/DJ/musicbot/com.user.musicbot.plist \
   ~/Library/LaunchAgents/

launchctl unload ~/Library/LaunchAgents/com.user.musicbot.plist
launchctl load   ~/Library/LaunchAgents/com.user.musicbot.plist
```

> On newer macOS you may see `load`/`unload` called deprecated. They still work.
> Modern equivalents:
> `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.musicbot.plist`
> and `launchctl bootout gui/$(id -u)/com.user.musicbot`.

---

## Check status & logs

```bash
# Alive if the first column is a PID (a number):
launchctl list | grep musicbot

# Follow the log (Ctrl-C to stop). Healthy startup prints:
#   Starting music bot. Authorized user IDs: [...]
tail -f /Users/orlandocedeno/Library/Logs/musicbot.log
```

## Update dependencies later

```bash
/Users/orlandocedeno/Music-Download-Bot/DJ/musicbot/venv/bin/python3 \
  -m pip install --upgrade -r \
  /Users/orlandocedeno/Music-Download-Bot/DJ/musicbot/requirements.txt
# then reload (see above)
```

---

## Using the bot

From Telegram, send the bot any of:

- a **YouTube** link (video, youtu.be, Music) → downloaded directly
- a **SoundCloud** link (track or public playlist) → downloaded directly
- a **Spotify** link (track, album, or playlist) → track info read, matching
  audio pulled from YouTube via spotdl (Spotify's own audio is DRM-protected;
  playlists/albums expand and download track by track)
- a **Shazam** link (shazam.com or shz.am share links) → resolved to a song
  name, then found on YouTube
- a link from **any other yt-dlp-supported site** (Bandcamp, Vimeo, Mixcloud,
  and hundreds more) → passed straight to music-dl; works if yt-dlp can extract
  audio from it
- a plain **song name** (e.g. `daft punk one more time`) → YouTube search,
  preferring lyric videos
- several links at once (one per line) → all downloaded, each reported
  separately

Every download lands as an MP3 in `DJ/Music/`, and a download archive prevents
re-downloading anything already in your library.

Commands: `/start`, `/help`, `/id` (shows your Telegram user ID for the allowlist).

Keep the Mac awake so downloads don't stall: run `caffeinate -i` in a Terminal
window, or set "prevent sleep" in System Settings.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| No reply at all | `launchctl list \| grep musicbot`; check the log. Usually a bad token in `.env`. |
| "Sorry — you're not authorized" | Add your ID (send `/id`) to `ALLOWED_USER_IDS` in `.env`, then reload. |
| Spotify links error about spotdl | `./venv/bin/python3 -m pip install spotdl` in the musicbot folder, then reload. |
| Downloads never finish | Mac is asleep — see `caffeinate` above. Long items may hit the 30-min timeout (`MUSIC_DL_TIMEOUT` in `.env`, seconds). |
| `launchctl load` says "already loaded" | Unload first, then load. |
| Moved/renamed the repo folder | The venv bakes absolute paths — recreate it: `rm -rf venv && python3 -m venv venv && ./venv/bin/python3 -m pip install -r requirements.txt`. Also re-copy the plist and reload. |

## Security notes

- The allowlist is enforced on every message (except `/id`, open so you can find
  your own ID). Non-allowed users are refused and never reach `music-dl`.
- Secrets stay in `.env` (gitignored) — never in git or the committed plist.
- If your token ever leaks, `/revoke` in @BotFather, paste the new token into
  `.env`, and reload.
