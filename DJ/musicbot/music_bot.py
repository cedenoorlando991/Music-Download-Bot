#!/usr/bin/env python3
"""
music_bot.py — Telegram bot that lets you text a YouTube URL (or a song name)
from your phone and download it on your home Mac by invoking your existing
`music-dl` bash script in one-shot mode.

Design notes
------------
* Uses python-telegram-bot v21+ (async) with LONG POLLING. This is outbound
  only: the bot dials out to Telegram's servers, so you do NOT need to open
  any ports, set up a webhook, or expose your Mac to the internet.
* Configuration comes from environment variables, loaded from a local .env
  file (next to this script) if present — see _load_dotenv below. The .env file
  is gitignored, so no secrets live in this file OR in the committed plist.
* An allowlist (ALLOWED_USER_IDS) is enforced on EVERY message and command.
  Anyone not on the list is politely refused and never reaches music-dl.
  Without this, anyone who discovered the bot could run downloads on your Mac.

Environment variables
----------------------
  TELEGRAM_BOT_TOKEN  (required)  Token from @BotFather.
  ALLOWED_USER_IDS    (required*) Comma-separated numeric Telegram user IDs.
                                  *Technically optional, but if empty NO ONE
                                  can use the bot except via /id (which is
                                  always allowed so you can discover your ID).
  MUSIC_DL            (required)  Absolute path to your music-dl script.
  MUSIC_DIR           (optional)  Absolute path to the Music output folder,
                                  used only to tell you where files landed and
                                  to detect the newest file after a download.
"""

import asyncio
import logging
import os
import re
import shlex
import time
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Configuration (read once at startup from the environment)
# --------------------------------------------------------------------------- #

def _load_dotenv() -> None:
    """
    Load KEY=VALUE pairs from a .env file into the environment WITHOUT
    overwriting variables already set (so launchd/shell values win). Looks for
    .env next to this script first, then in the current working directory.

    Dependency-free (works in any venv). Supports blank lines, '# comments',
    optional 'export ' prefixes, and single/double-quoted values. Keeping
    secrets in .env (gitignored) means they never enter git or the plist.
    """
    for candidate in (Path(__file__).resolve().parent / ".env", Path.cwd() / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, sep, val = line.partition("=")
            if not sep:
                continue
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
        break  # first .env found wins


_load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
MUSIC_DL = os.environ.get("MUSIC_DL", "").strip()
MUSIC_DIR = os.environ.get("MUSIC_DIR", "").strip()

# How long (seconds) to let a single music-dl invocation run before giving up.
# A long song / slow network can take a while, so this is generous.
SUBPROCESS_TIMEOUT = int(os.environ.get("MUSIC_DL_TIMEOUT", "1800"))  # 30 min

# Telegram messages cap out at 4096 chars; keep our replies safely under that.
MAX_TG_MESSAGE = 3500


def _parse_allowed_ids(raw: str) -> set[int]:
    """Parse a comma-separated list of numeric Telegram user IDs into a set."""
    ids: set[int] = set()
    for chunk in raw.replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            logging.warning("Ignoring non-numeric ALLOWED_USER_IDS entry: %r", chunk)
    return ids


ALLOWED_USER_IDS = _parse_allowed_ids(os.environ.get("ALLOWED_USER_IDS", ""))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("music_bot")


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #

def is_authorized(update: Update) -> bool:
    """Return True only if the sender's numeric ID is on the allowlist."""
    user = update.effective_user
    return user is not None and user.id in ALLOWED_USER_IDS


async def deny(update: Update) -> None:
    """Politely refuse an unauthorized user (and log the attempt)."""
    user = update.effective_user
    uid = user.id if user else "unknown"
    log.warning("Denied message from unauthorized user id=%s", uid)
    if update.effective_message:
        await update.effective_message.reply_text(
            "Sorry — you're not authorized to use this bot.\n"
            f"Your Telegram user ID is {uid}. "
            "Ask the owner to add it to the allowlist."
        )


# --------------------------------------------------------------------------- #
# music-dl invocation
# --------------------------------------------------------------------------- #

def _newest_music_file(since: float) -> str | None:
    """
    Return the name of the newest audio file in MUSIC_DIR modified at or after
    `since` (a time.time() timestamp), or None. Best-effort convenience only.
    """
    if not MUSIC_DIR:
        return None
    music_path = Path(MUSIC_DIR)
    if not music_path.is_dir():
        return None
    newest_name: str | None = None
    newest_mtime = since - 1  # accept files touched during the run
    for entry in music_path.iterdir():
        if entry.suffix.lower() not in {".mp3", ".m4a", ".opus", ".flac", ".wav"}:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= since and mtime > newest_mtime:
            newest_mtime = mtime
            newest_name = entry.name
    return newest_name


async def run_music_dl(arg: str) -> tuple[int, str, str]:
    """
    Run `music-dl <arg>` in one-shot mode as a subprocess.

    Returns (return_code, stdout, stderr). return_code is -1 on timeout.
    """
    # NOTE: we pass MUSIC_DL and arg as separate argv entries (no shell), so a
    # song title with spaces/quotes is delivered to music-dl as a single
    # argument and there is no shell-injection surface.
    log.info("Running: %s %s", MUSIC_DL, shlex.quote(arg))
    proc = await asyncio.create_subprocess_exec(
        MUSIC_DL,
        arg,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Run inside the script's own directory so its relative ./Music and
        # ./.download-archive.txt resolve the way they do when run by hand.
        cwd=str(Path(MUSIC_DL).resolve().parent),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.error("music-dl timed out after %ss; killing.", SUBPROCESS_TIMEOUT)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return -1, "", f"Timed out after {SUBPROCESS_TIMEOUT}s."

    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")
    return proc.returncode if proc.returncode is not None else -1, stdout, stderr


# Audio extensions we consider a "final" music file.
_AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".flac", ".wav", ".aac", ".ogg"}

# Ordered by confidence. Each pattern captures a file path/name from a line of
# music-dl / yt-dlp / ffmpeg output. We prefer patterns that name the FINAL
# transcoded file (music-dl's own "ok" line, yt-dlp's MoveFiles/ExtractAudio)
# over the raw [download] Destination (which is often the pre-transcode
# container like .webm/.m4a). Group 1 is always the path/name.
_FINAL_FILE_PATTERNS = [
    # music-dl success line, e.g.:  ok "Artist - Title.mp3"
    re.compile(r'\bok\s+"([^"]+)"'),
    # yt-dlp moving the finished file into place:
    #   [MoveFiles] Moving file "tmp.mp3" to "Artist - Title.mp3"
    re.compile(r'\[MoveFiles\][^\n]*?\bto\s+"([^"]+)"'),
    # yt-dlp audio-extraction destination:
    #   [ExtractAudio] Destination: Artist - Title.mp3
    re.compile(r'\[ExtractAudio\]\s+Destination:\s*(.+)', re.MULTILINE),
]

# Lower-confidence: the raw download destination (may be a temp container).
_DOWNLOAD_DEST_PATTERN = re.compile(
    r'\[download\]\s+Destination:\s*(.+)', re.MULTILINE
)

# Lines that indicate the track was already in the archive and skipped.
_ALREADY_PATTERNS = [
    re.compile(r'has already been downloaded', re.IGNORECASE),
    re.compile(r'already been recorded in the archive', re.IGNORECASE),
    re.compile(r'already in (?:your )?library', re.IGNORECASE),
]


def extract_downloaded_filenames(stdout: str, stderr: str) -> list[str]:
    """
    Parse music-dl / yt-dlp output and return the basename(s) of the audio
    file(s) that were saved, in the order they appear. One invocation can
    produce several files (e.g. a playlist URL), so this returns a list.

    Robustness: tries the high-confidence "final file" patterns first. If none
    match, it falls back to the raw [download] Destination line, swapping the
    extension to .mp3 (music-dl always transcodes to mp3) as a best guess.
    Returns [] if nothing usable can be extracted — the caller then degrades to
    an mtime scan or a generic success message.
    """
    text = stdout + "\n" + stderr
    ordered: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        name = os.path.basename(raw.strip().strip('"').rstrip("."))
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)

    # High-confidence final-file patterns.
    for pattern in _FINAL_FILE_PATTERNS:
        for match in pattern.finditer(text):
            add(match.group(1))

    # Keep only real audio files if we found any; those are the finished tracks.
    audio = [n for n in ordered if os.path.splitext(n)[1].lower() in _AUDIO_EXTS]
    if audio:
        return audio

    # Fallback: raw download destinations, normalized to .mp3.
    fallback: list[str] = []
    fseen: set[str] = set()
    for match in _DOWNLOAD_DEST_PATTERN.finditer(text):
        base = os.path.basename(match.group(1).strip().strip('"'))
        if not base:
            continue
        stem, ext = os.path.splitext(base)
        guess = base if ext.lower() in _AUDIO_EXTS else stem + ".mp3"
        if guess not in fseen:
            fseen.add(guess)
            fallback.append(guess)
    return fallback


def is_already_in_library(stdout: str, stderr: str) -> bool:
    """True if the output says every requested track was already archived."""
    text = stdout + "\n" + stderr
    return any(p.search(text) for p in _ALREADY_PATTERNS)


def summarize_result(rc: int, stdout: str, stderr: str, newest: str | None) -> str:
    """Turn music-dl's output into a short, friendly reply for the user."""
    if rc == -1:
        return "⏱️ Timed out. The download took too long and was stopped."

    if rc == 0:
        filenames = extract_downloaded_filenames(stdout, stderr)

        if filenames:
            # We know exactly what was saved — name each file.
            if len(filenames) == 1:
                return f"✅ Done! Saved:\n{filenames[0]}"
            listed = "\n".join(f"• {n}" for n in filenames)
            return f"✅ Done! Saved {len(filenames)} tracks:\n{listed}"

        # No filename in the output. If it was an archive skip, say so.
        if is_already_in_library(stdout, stderr):
            return "✅ Already in your library — skipped (no re-download)."

        # Last resort: the newest file that appeared in MUSIC_DIR during the run.
        if newest:
            return f"✅ Done! Saved:\n{newest}"

        return "✅ Done! Download finished."

    # Non-zero exit: surface the tail of stderr/stdout so the user has a clue.
    detail = (stderr.strip() or stdout.strip() or "no output").splitlines()
    tail = "\n".join(detail[-8:])[:1500]
    return f"❌ music-dl failed (exit {rc}):\n{tail}"


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

HELP_TEXT = (
    "🎵 *Music download bot*\n\n"
    "Send me a YouTube link and I'll download it as an MP3 on the home Mac.\n\n"
    "You can also:\n"
    "• Send a song name (e.g. `daft punk one more time`) — I'll search YouTube.\n"
    "• Send several links at once, one per line — I'll grab them all.\n\n"
    "Commands:\n"
    "/start, /help — this message\n"
    "/id — show your Telegram user ID (needed for the allowlist)"
)


async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/start and /help — usage info. Gated by the allowlist."""
    if not is_authorized(update):
        await deny(update)
        return
    await update.effective_message.reply_markdown(HELP_TEXT)


async def cmd_id(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /id — reply with the sender's numeric Telegram user ID.

    This is intentionally allowed for EVERYONE (no allowlist check) so that a
    new owner can start the bot and discover their own ID during setup.
    """
    user = update.effective_user
    uid = user.id if user else "unknown"
    await update.effective_message.reply_text(
        f"Your Telegram user ID is: {uid}\n"
        "Put this in ALLOWED_USER_IDS to authorize yourself."
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Any non-command text message: treat it as input for music-dl."""
    if not is_authorized(update):
        await deny(update)
        return

    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        await message.reply_text("Send me a YouTube link or a song name.")
        return

    # If the message contains URLs, treat each whitespace/newline-separated URL
    # as its own download. Otherwise treat the whole message as a search query.
    tokens = text.split()
    urls = [t for t in tokens if t.startswith("http://") or t.startswith("https://")]
    args = urls if urls else [text]

    # Immediate acknowledgment so the user knows we're on it.
    ack = "⬇️ Downloading…" if len(args) == 1 else f"⬇️ Downloading {len(args)} items…"
    await message.reply_text(ack)
    await ctx.bot.send_chat_action(message.chat_id, ChatAction.TYPING)

    for i, arg in enumerate(args, start=1):
        prefix = f"[{i}/{len(args)}] " if len(args) > 1 else ""
        started = time.time()
        try:
            rc, out, err = await run_music_dl(arg)
        except FileNotFoundError:
            await message.reply_text(
                f"{prefix}❌ Could not run music-dl.\n"
                f"MUSIC_DL is set to: {MUSIC_DL or '(unset)'}\n"
                "Check that the path is correct and the file is executable."
            )
            continue
        except Exception as exc:  # defensive: never let the handler crash
            log.exception("Unexpected error running music-dl")
            await message.reply_text(f"{prefix}❌ Unexpected error: {exc}")
            continue

        newest = _newest_music_file(started)
        reply = summarize_result(rc, out, err, newest)
        await message.reply_text((prefix + reply)[:MAX_TG_MESSAGE])


# --------------------------------------------------------------------------- #
# Startup
# --------------------------------------------------------------------------- #

def _validate_config() -> None:
    """Fail fast with a clear message if required config is missing."""
    problems = []
    if not TELEGRAM_BOT_TOKEN:
        problems.append("TELEGRAM_BOT_TOKEN is not set.")
    if not MUSIC_DL:
        problems.append("MUSIC_DL is not set.")
    elif not Path(MUSIC_DL).exists():
        problems.append(f"MUSIC_DL path does not exist: {MUSIC_DL}")
    elif not os.access(MUSIC_DL, os.X_OK):
        problems.append(f"MUSIC_DL is not executable: {MUSIC_DL} (try: chmod +x)")
    if not ALLOWED_USER_IDS:
        # Not fatal — /id still works so the owner can bootstrap — but warn loudly.
        log.warning(
            "ALLOWED_USER_IDS is empty: no one can download yet. "
            "Send /id to the bot to find your ID, then set it in the plist."
        )
    if problems:
        for p in problems:
            log.error("CONFIG ERROR: %s", p)
        raise SystemExit(1)


def main() -> None:
    _validate_config()

    log.info("Starting music bot. Authorized user IDs: %s", sorted(ALLOWED_USER_IDS))
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # --- Python 3.14 compatibility shim ---------------------------------- #
    # python-telegram-bot 21.6's run_polling() calls asyncio.get_event_loop()
    # internally. On Python 3.14 get_event_loop() no longer auto-creates a loop
    # when none exists in the main thread — it raises
    # "RuntimeError: There is no current event loop in thread 'MainThread'".
    # Ensure a current loop exists before we hand control to PTB so
    # get_event_loop() returns it instead of raising. Harmless on older Pythons.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    # --------------------------------------------------------------------- #

    # run_polling blocks forever, handling long-polling + graceful shutdown.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
