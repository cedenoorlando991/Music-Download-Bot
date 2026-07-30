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
  MUSIC_DIR           (optional)  Absolute path to the Music output folder.
                                  Used to report saved filenames, to detect the
                                  newest file after a download, and as the
                                  destination for Spotify downloads via spotdl.
  SPOTDL              (optional)  Absolute path to the spotdl executable. If
                                  unset, we look next to this Python (the venv's
                                  bin/spotdl) and then fall back to PATH.

Link routing
------------
Any link (or bare song name) you send is routed automatically:
  * YouTube / SoundCloud / other yt-dlp sites -> downloaded directly by music-dl.
  * Spotify links -> spotdl reads the track metadata and downloads the matching
    audio from YouTube (Spotify audio itself is DRM-protected and cannot be
    downloaded). spotdl must be installed in the venv (pip install spotdl).
  * Shazam links -> we resolve the link to "artist - title" and then run a
    YouTube search through music-dl.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import sys
import time
import urllib.request
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


def _discover_spotdl() -> str:
    """
    Locate the spotdl executable. Prefer the one installed alongside this
    Python (i.e. inside the same venv), since that's where `pip install spotdl`
    in the venv puts it; otherwise fall back to whatever is on PATH.
    """
    explicit = os.environ.get("SPOTDL", "").strip()
    if explicit:
        return explicit
    candidate = Path(sys.executable).parent / "spotdl"
    if candidate.exists():
        return str(candidate)
    return "spotdl"  # rely on PATH; may not exist (handled at call time)


SPOTDL = _discover_spotdl()


def music_output_dir() -> str:
    """
    Where downloaded audio should live. Uses MUSIC_DIR if set, else the ./Music
    folder next to the music-dl script (matching music-dl's own default).
    """
    if MUSIC_DIR:
        return MUSIC_DIR
    if MUSIC_DL:
        return str(Path(MUSIC_DL).resolve().parent / "Music")
    return os.getcwd()

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


async def _run_subprocess(argv: list[str], cwd: str) -> tuple[int, str, str]:
    """
    Run a command (no shell) and capture output.

    Returns (return_code, stdout, stderr); return_code is -1 on timeout.
    Passing argv as a list means arguments with spaces/quotes are delivered
    verbatim — there is no shell-injection surface from message text.
    """
    log.info("Running: %s", " ".join(shlex.quote(a) for a in argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.error("Command timed out after %ss; killing.", SUBPROCESS_TIMEOUT)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return -1, "", f"Timed out after {SUBPROCESS_TIMEOUT}s."

    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")
    return proc.returncode if proc.returncode is not None else -1, stdout, stderr


async def run_music_dl(arg: str) -> tuple[int, str, str]:
    """
    Run `music-dl <arg>` in one-shot mode. `arg` is a direct URL (YouTube,
    SoundCloud, …) or a search string. music-dl runs from its own directory so
    its relative ./Music and ./.download-archive.txt resolve as they do by hand.
    """
    return await _run_subprocess(
        [MUSIC_DL, arg],
        cwd=str(Path(MUSIC_DL).resolve().parent),
    )


async def run_spotdl(url: str) -> tuple[int, str, str]:
    """
    Download a Spotify track/album/playlist via spotdl. spotdl reads the
    Spotify metadata and fetches the matching audio from YouTube, saving mp3s
    into MUSIC_DIR (Spotify's own audio is DRM-protected and can't be pulled).
    """
    outdir = music_output_dir()
    os.makedirs(outdir, exist_ok=True)
    # Force mp3 and a clean "Artist - Title.mp3" name, saved into the music dir.
    output_template = os.path.join(outdir, "{artists} - {title}.{output-ext}")
    argv = [
        SPOTDL,
        "download",
        url,
        "--format",
        "mp3",
        "--output",
        output_template,
    ]
    return await _run_subprocess(argv, cwd=outdir)


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
# Spotify (spotdl) output parsing
# --------------------------------------------------------------------------- #

# spotdl prints one line per track, e.g.:  Downloaded "Artist - Title"
_SPOTDL_DOWNLOADED = re.compile(r'Downloaded\s+"([^"]+)"')
# and for files it skips:  Skipping Artist - Title (file already exists)
_SPOTDL_SKIPPED = re.compile(r'Skipping\s+(.+?)\s+\(file already exists', re.IGNORECASE)


def summarize_spotdl(rc: int, stdout: str, stderr: str) -> str:
    """Turn spotdl's output into a friendly reply, naming each saved track."""
    if rc == -1:
        return "⏱️ Timed out. The Spotify download took too long and was stopped."

    text = stdout + "\n" + stderr
    downloaded = [m.group(1) for m in _SPOTDL_DOWNLOADED.finditer(text)]
    skipped = [m.group(1) for m in _SPOTDL_SKIPPED.finditer(text)]

    # spotdl's "Downloaded" names have no extension; we save as .mp3.
    names = [n if n.lower().endswith(".mp3") else f"{n}.mp3" for n in downloaded]

    if rc == 0:
        if names:
            if len(names) == 1:
                return f"✅ Done (via Spotify → YouTube)! Saved:\n{names[0]}"
            listed = "\n".join(f"• {n}" for n in names)
            return f"✅ Done (via Spotify → YouTube)! Saved {len(names)} tracks:\n{listed}"
        if skipped:
            return "✅ Already in your library — skipped (no re-download)."
        return "✅ Done! Spotify download finished."

    # Failure: surface the tail so the user has a clue.
    detail = (stderr.strip() or stdout.strip() or "no output").splitlines()
    tail = "\n".join(detail[-8:])[:1500]
    return f"❌ spotdl failed (exit {rc}):\n{tail}"


# --------------------------------------------------------------------------- #
# Shazam link resolution  (link -> "artist title" search string)
# --------------------------------------------------------------------------- #

_SHAZAM_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_OG_TITLE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _search_json_for_recording(obj) -> str | None:
    """Recursively look for a MusicRecording-like dict and build 'artist title'."""
    if isinstance(obj, dict):
        types = obj.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(t in ("MusicRecording", "MusicComposition", "Song") for t in types):
            name = obj.get("name")
            artist = obj.get("byArtist")
            if isinstance(artist, dict):
                artist = artist.get("name")
            elif isinstance(artist, list) and artist:
                first = artist[0]
                artist = first.get("name") if isinstance(first, dict) else first
            if name:
                return f"{artist} {name}".strip() if artist else str(name)
        for value in obj.values():
            found = _search_json_for_recording(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _search_json_for_recording(item)
            if found:
                return found
    return None


def resolve_shazam(url: str) -> str | None:
    """
    Turn a Shazam link into an "artist title" search string. Shazam is a song
    ID service, not an audio source, so we scrape the shared page's metadata and
    then let music-dl find the track on YouTube.

    Strategy (most reliable first): JSON-LD MusicRecording -> og:title ->
    <title> tag. Returns None if nothing usable can be extracted.
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            html_text = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # network/HTTP errors — degrade gracefully
        log.warning("Shazam fetch failed for %s: %s", url, exc)
        return None

    # 1) JSON-LD blocks (most structured/reliable).
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_text,
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        found = _search_json_for_recording(data)
        if found:
            return _clean_query(found)

    # 2) og:title meta.
    m = _OG_TITLE.search(html_text)
    if m:
        return _clean_query(m.group(1))

    # 3) <title> tag, e.g. "Song - Artist | Shazam".
    m = _SHAZAM_TITLE_TAG.search(html_text)
    if m:
        title = re.sub(r"\s*\|\s*Shazam.*$", "", m.group(1), flags=re.IGNORECASE)
        return _clean_query(title.replace(" - ", " "))

    return None


def _clean_query(text: str) -> str:
    """Normalize an extracted title into a plain search string."""
    import html as _html

    text = _html.unescape(text).strip()
    text = re.sub(r"\s+", " ", text)
    return text


# --------------------------------------------------------------------------- #
# URL routing
# --------------------------------------------------------------------------- #

def is_url(token: str) -> bool:
    return token.startswith(("http://", "https://", "spotify:"))


def classify(token: str) -> tuple[str, str]:
    """
    Decide how to handle one token. Returns (kind, value):
      "spotify" -> download via spotdl
      "shazam"  -> resolve link, then YouTube-search via music-dl
      "direct"  -> hand the URL straight to music-dl (YouTube/SoundCloud/etc.)
      "search"  -> treat the text as a YouTube search query for music-dl
    """
    low = token.lower()
    if not is_url(token):
        return ("search", token)
    if "open.spotify.com" in low or low.startswith("spotify:"):
        return ("spotify", token)
    if "shazam.com" in low or "shz.am" in low:
        return ("shazam", token)
    return ("direct", token)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

HELP_TEXT = (
    "🎵 *Music download bot*\n\n"
    "Paste a link and I'll download it as an MP3 on the home Mac:\n"
    "• *YouTube* / *SoundCloud* — downloaded directly.\n"
    "• *Spotify* — I read the track info and grab the matching YouTube audio.\n"
    "• *Shazam* — I look up the song, then grab it from YouTube.\n\n"
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


async def process_job(kind: str, value: str) -> str:
    """Run one download job and return the reply text. Never raises."""
    try:
        if kind == "spotify":
            try:
                rc, out, err = await run_spotdl(value)
            except FileNotFoundError:
                return (
                    "❌ Spotify links need spotdl, which isn't installed.\n"
                    "Install it in the bot's venv:\n"
                    "  <venv>/bin/python3 -m pip install spotdl"
                )
            return summarize_spotdl(rc, out, err)

        if kind == "shazam":
            query = await asyncio.to_thread(resolve_shazam, value)
            if not query:
                return (
                    "❌ Couldn't read that Shazam link. Try opening it and "
                    "sending me the song name instead."
                )
            started = time.time()
            rc, out, err = await run_music_dl(query)
            base = summarize_result(rc, out, err, _newest_music_file(started))
            return f"🎧 Shazam → {query}\n{base}"

        # "direct" (YouTube/SoundCloud/etc.) or "search" — both go to music-dl.
        try:
            started = time.time()
            rc, out, err = await run_music_dl(value)
        except FileNotFoundError:
            return (
                "❌ Could not run music-dl.\n"
                f"MUSIC_DL is set to: {MUSIC_DL or '(unset)'}\n"
                "Check that the path is correct and the file is executable."
            )
        return summarize_result(rc, out, err, _newest_music_file(started))

    except Exception as exc:  # defensive: never let a job crash the handler
        log.exception("Unexpected error in job %s(%r)", kind, value)
        return f"❌ Unexpected error: {exc}"


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Any non-command text message: route each link/query to the right downloader."""
    if not is_authorized(update):
        await deny(update)
        return

    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        await message.reply_text("Send me a link (YouTube/SoundCloud/Spotify/Shazam) or a song name.")
        return

    # If the message contains links, handle each one; otherwise treat the whole
    # message as a single search query.
    tokens = text.split()
    links = [t for t in tokens if is_url(t)]
    jobs = [classify(t) for t in links] if links else [classify(text)]

    # Immediate acknowledgment so the user knows we're on it.
    ack = "⬇️ Downloading…" if len(jobs) == 1 else f"⬇️ Downloading {len(jobs)} items…"
    await message.reply_text(ack)
    await ctx.bot.send_chat_action(message.chat_id, ChatAction.TYPING)

    for i, (kind, value) in enumerate(jobs, start=1):
        prefix = f"[{i}/{len(jobs)}] " if len(jobs) > 1 else ""
        reply = await process_job(kind, value)
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
