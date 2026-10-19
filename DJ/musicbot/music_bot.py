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
  MUSIC_DIR           (optional)  Default Music output folder (owner library).
  DALIZAS_MUSIC_DIR   (optional)  Separate library for DALIZAS_USER_ID.
  DALIZAS_USER_ID     (optional)  Telegram user ID whose downloads go to
                                  DALIZAS_MUSIC_DIR instead of MUSIC_DIR.
  CLAIRE_MUSIC_DIR    (optional)  Separate library for CLAIRE_USER_ID.
  CLAIRE_USER_ID      (optional)  Telegram user ID whose downloads go to
                                  CLAIRE_MUSIC_DIR instead of MUSIC_DIR.
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
  * Shazam links -> we resolve the link to "artist - title" (plus Apple's
    genre and release year) via Shazam's track API, then run a YouTube search
    through music-dl.

Genre folders
-------------
Downloads land in <library>/All Songs/ and are hard-linked into a
"<Genre> <Decade>/" folder (see library.py). Each reply shows the genre with a
button to correct it; /unknown walks through songs nobody could identify.

On the first download of each calendar day the bot upgrades Homebrew yt-dlp
and the venv's yt-dlp + spotdl (YouTube breaks old extractors often). Later
downloads that day skip the check. Stamp file: .deps-checked next to this
script.
"""

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import library

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
DALIZAS_MUSIC_DIR = os.environ.get("DALIZAS_MUSIC_DIR", "").strip()
CLAIRE_MUSIC_DIR = os.environ.get("CLAIRE_MUSIC_DIR", "").strip()

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
    Default library (owner). Uses MUSIC_DIR if set, else the ./Music folder
    next to the music-dl script (matching music-dl's own default).
    """
    if MUSIC_DIR:
        return MUSIC_DIR
    if MUSIC_DL:
        return str(Path(MUSIC_DL).resolve().parent / "Music")
    return os.getcwd()


def _parse_one_id(raw: str) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logging.warning("Ignoring non-numeric per-user library ID: %r", raw)
        return None


DALIZAS_USER_ID = _parse_one_id(os.environ.get("DALIZAS_USER_ID", ""))
CLAIRE_USER_ID = _parse_one_id(os.environ.get("CLAIRE_USER_ID", ""))


def music_dir_for_user(user_id: int | None) -> str:
    """Pick the library folder for this Telegram user."""
    if user_id is not None:
        if user_id == DALIZAS_USER_ID and DALIZAS_MUSIC_DIR:
            return DALIZAS_MUSIC_DIR
        if user_id == CLAIRE_USER_ID and CLAIRE_MUSIC_DIR:
            return CLAIRE_MUSIC_DIR
    return music_output_dir()


def songs_dir(music_dir: str) -> str:
    """Where downloads land inside a library: <library>/All Songs."""
    return str(Path(music_dir) / library.ALL_SONGS)


def archive_for_music_dir(music_dir: str) -> str:
    """
    Per-library yt-dlp archive so two users downloading the same video don't
    skip each other. The owner keeps the historical file next to music-dl.
    """
    default = (
        str(Path(MUSIC_DL).resolve().parent / ".download-archive.txt")
        if MUSIC_DL
        else str(Path(music_dir) / ".download-archive.txt")
    )
    for user_dir in (DALIZAS_MUSIC_DIR, CLAIRE_MUSIC_DIR):
        if not user_dir:
            continue
        try:
            if Path(music_dir).resolve() == Path(user_dir).resolve():
                return str(Path(user_dir) / ".download-archive.txt")
        except OSError:
            pass
    return default

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

def audio_names(music_dir: str) -> set[str]:
    """Basenames of the audio files currently in `music_dir`."""
    try:
        return {e.name for e in Path(music_dir).iterdir() if e.suffix.lower() in _AUDIO_EXTS}
    except OSError:
        return set()


def new_audio_files(before: set[str], music_dir: str) -> list[str]:
    """
    Audio files in `music_dir` that weren't there in the `before` snapshot.
    Used when the downloader's stdout doesn't name the saved file. (Not an
    mtime check: writing a genre tag touches the previous song's mtime, so a
    multi-link message would re-report — and re-file — it.)
    """
    return sorted(audio_names(music_dir) - before)


def display_saved_path(music_dir: str, filename: str) -> str:
    """Path inside the library, e.g. All Songs/Artist - Title.mp3."""
    return f"{library.ALL_SONGS}/{os.path.basename(filename)}"


def format_saved_reply(
    music_dir: str,
    filenames: list[str],
    *,
    via: str | None = None,
) -> str:
    paths = [display_saved_path(music_dir, n) for n in filenames]
    if via == "spotify":
        head = "✅ Done (via Spotify → YouTube)! Saved:"
    else:
        head = "✅ Done! Saved:"
    if len(paths) == 1:
        return f"{head}\n{paths[0]}"
    listed = "\n".join(f"• {p}" for p in paths)
    return f"{head} {len(paths)} tracks:\n{listed}"


def log_job_output(tool: str, rc: int, stdout: str, stderr: str) -> None:
    """Write the downloader's tail to the bot log so `tail -f` is useful."""
    combined = _strip_ansi(f"{stdout}\n{stderr}").strip()
    lines = combined.splitlines()
    tail = "\n".join(lines[-50:]) if lines else "(no output)"
    log.info("%s finished rc=%s\n%s", tool, rc, tail)


def _merge_names(*groups: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group:
            name = os.path.basename(raw.strip())
            if name and name not in seen:
                seen.add(name)
                ordered.append(name)
    return ordered


async def _run_subprocess(
    argv: list[str],
    cwd: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """
    Run a command (no shell) and capture output.

    Returns (return_code, stdout, stderr); return_code is -1 on timeout.
    Passing argv as a list means arguments with spaces/quotes are delivered
    verbatim — there is no shell-injection surface from message text.
    extra_env overlays the current environment for this child only (so two
    users can download into different folders at the same time).
    """
    log.info("Running: %s", " ".join(shlex.quote(a) for a in argv))
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
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


async def run_music_dl(arg: str, music_dir: str) -> tuple[int, str, str]:
    """
    Run `music-dl <arg>` in one-shot mode. `arg` is a direct URL (YouTube,
    SoundCloud, …) or a search string. music-dl runs from its own directory so
    its relative paths resolve as they do by hand; MUSIC_DIR / ARCHIVE are
    passed in the child env so this user's library (its All Songs/) is used.
    """
    extra_env = {
        "MUSIC_DIR": songs_dir(music_dir),
        "ARCHIVE": archive_for_music_dir(music_dir),
    }
    return await _run_subprocess(
        [MUSIC_DL, arg],
        cwd=str(Path(MUSIC_DL).resolve().parent),
        extra_env=extra_env,
    )


async def run_spotdl(url: str, music_dir: str) -> tuple[int, str, str]:
    """
    Download a Spotify track/album/playlist via spotdl. spotdl reads the
    Spotify metadata and fetches the matching audio from YouTube, saving mp3s
    into this user's library (Spotify's own audio is DRM-protected and can't
    be pulled).
    """
    outdir = songs_dir(music_dir)
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
        "--simple-tui",
        "--print-errors",
        "--log-level",
        "INFO",
    ]
    return await _run_subprocess(argv, cwd=outdir)


# Audio extensions we consider a "final" music file.
_AUDIO_EXTS = library.AUDIO_EXTS

# Ordered by confidence. Each pattern captures a file path/name from a line of
# music-dl / yt-dlp / ffmpeg output. We prefer patterns that name the FINAL
# transcoded file (music-dl's own "ok" line, yt-dlp's MoveFiles/ExtractAudio)
# over the raw [download] Destination (which is often the pre-transcode
# container like .webm/.m4a). Group 1 is always the path/name.
_FINAL_FILE_PATTERNS = [
    # music-dl success line, e.g.:  * Artist - Title.mp3  (or older: ok "…")
    re.compile(r'\bok\s+"([^"]+)"'),
    re.compile(r'^\*\s+(.+\.(?:mp3|m4a|opus|flac|wav))\s*$', re.MULTILINE | re.IGNORECASE),
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


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _error_lines(stdout: str, stderr: str) -> list[str]:
    """
    Pull yt-dlp / spotdl `ERROR:` lines out of combined output.

    music-dl tees yt-dlp into stdout, then writes a generic `x failed: <query>`
    to stderr. Preferring stderr alone hid the real reason in Telegram.
    """
    found: list[str] = []
    seen: set[str] = set()
    for raw in (stdout + "\n" + stderr).splitlines():
        line = _strip_ansi(raw).strip()
        if not re.match(r"^ERROR:", line, re.IGNORECASE):
            continue
        line = re.sub(r"^ERROR:\s*", "", line, flags=re.IGNORECASE)
        if line and line not in seen:
            seen.add(line)
            found.append(line)
    return found


def explain_download_failure(tool: str, rc: int, stdout: str, stderr: str) -> str:
    """Turn a failed music-dl / spotdl run into a clear Telegram reply."""
    errors = _error_lines(stdout, stderr)
    blob = "\n".join(errors).lower() if errors else _strip_ansi(stdout + "\n" + stderr).lower()

    if "http error 403" in blob or "403: forbidden" in blob:
        return (
            "❌ YouTube refused the download (HTTP 403).\n"
            "This usually means yt-dlp is out of date.\n\n"
            "On the Mac, run:\n"
            "  brew upgrade yt-dlp\n"
            "then send the song again."
        )
    if "sign in to confirm" in blob or "not a bot" in blob:
        return (
            "❌ YouTube asked for a sign-in (bot check).\n"
            "Try again later, or paste a different link."
        )
    if "private video" in blob or "video unavailable" in blob:
        extra = errors[-1] if errors else "Private, deleted, or region-blocked."
        return f"❌ That video isn't available.\n{extra}"
    if "unable to download webpage" in blob or "network is unreachable" in blob:
        extra = errors[-1] if errors else "Check the Mac's internet and try again."
        return f"❌ Network error talking to YouTube.\n{extra}"

    if errors:
        return f"❌ {tool} failed:\n" + "\n".join(errors[-3:])[:1500]

    useful = []
    for raw in (stdout + "\n" + stderr).splitlines():
        line = _strip_ansi(raw).strip()
        if not line or re.match(r"^x failed:", line):
            continue
        useful.append(line)
    tail = "\n".join(useful[-6:])[:1500] if useful else "no output from the downloader"
    return f"❌ {tool} failed (exit {rc}):\n{tail}"


def summarize_result(
    rc: int,
    stdout: str,
    stderr: str,
    found: list[str],
    music_dir: str,
) -> tuple[str, list[str]]:
    """
    Turn music-dl's output into a short, friendly reply for the user, plus the
    names of the files it saved (empty unless something new landed).
    """
    if rc == -1:
        return "⏱️ Timed out. The download took too long and was stopped.", []

    if rc == 0:
        filenames = _merge_names(extract_downloaded_filenames(stdout, stderr), found)
        filenames = [n for n in filenames if (Path(songs_dir(music_dir)) / n).is_file()]

        if filenames:
            return format_saved_reply(music_dir, filenames), filenames

        # No filename in the output. If it was an archive skip, say so.
        if is_already_in_library(stdout, stderr):
            folder = Path(music_dir).name if music_dir else "Music"
            return f"✅ Already in your library — skipped (no re-download).\nLooked in: {folder}/", []

        folder = Path(music_dir).name if music_dir else "Music"
        tail = "\n".join(_strip_ansi(stdout + "\n" + stderr).splitlines()[-8:])[:800]
        return (
            f"❌ music-dl finished, but no new file appeared in {folder}/.\n"
            f"{tail or 'no output'}"
        ), []

    log.warning(
        "music-dl failed rc=%s: %s",
        rc,
        "; ".join(_error_lines(stdout, stderr)) or "no ERROR line",
    )
    return explain_download_failure("music-dl", rc, stdout, stderr), []


# --------------------------------------------------------------------------- #
# Spotify (spotdl) output parsing
# --------------------------------------------------------------------------- #

# spotdl prints one line per track, e.g.:  Downloaded "Artist - Title": <url>
_SPOTDL_DOWNLOADED = re.compile(r'Downloaded\s+"([^"]+)"')
# and for files it skips:  Skipping Artist - Title (file already exists)
_SPOTDL_SKIPPED = re.compile(r'Skipping\s+(.+?)\s+\(file already exists', re.IGNORECASE)


def summarize_spotdl(
    rc: int,
    stdout: str,
    stderr: str,
    found: list[str],
    music_dir: str,
) -> tuple[str, list[str]]:
    """Turn spotdl's output into a friendly reply, naming each saved track."""
    if rc == -1:
        return "⏱️ Timed out. The Spotify download took too long and was stopped.", []

    text = _strip_ansi(stdout + "\n" + stderr)
    downloaded = [m.group(1) for m in _SPOTDL_DOWNLOADED.finditer(text)]
    skipped = [m.group(1) for m in _SPOTDL_SKIPPED.finditer(text)]

    # spotdl's "Downloaded" names have no extension; we save as .mp3.
    names = [n if n.lower().endswith(".mp3") else f"{n}.mp3" for n in downloaded]
    names = [n for n in _merge_names(names, found) if (Path(songs_dir(music_dir)) / n).is_file()]

    if rc == 0:
        if names:
            return format_saved_reply(music_dir, names, via="spotify"), names
        if skipped:
            skip_names = [n if n.lower().endswith(".mp3") else f"{n}.mp3" for n in skipped]
            folder = Path(music_dir).name if music_dir else "Music"
            listed = "\n".join(f"• {folder}/{os.path.basename(n)}" for n in skip_names)
            return f"✅ Already in your library — skipped.\n{listed}", []
        folder = Path(music_dir).name if music_dir else "Music"
        tail = "\n".join(text.splitlines()[-8:])[:800]
        return (
            f"❌ Spotify download finished, but no new file appeared in {folder}/.\n"
            f"{tail or 'no output'}"
        ), []

    log.warning(
        "spotdl failed rc=%s: %s",
        rc,
        "; ".join(_error_lines(stdout, stderr)) or "no ERROR line",
    )
    return explain_download_failure("spotdl", rc, stdout, stderr), []


# --------------------------------------------------------------------------- #
# Shazam link resolution  (link -> artist, title, genre, year)
# --------------------------------------------------------------------------- #

# shazam.com answers page scrapes with HTTP 405 (bot wall), but its public
# discovery API still serves the same track JSON the website renders from.
SHAZAM_TRACK_API = "https://cdn.shazam.com/discovery/v5/en-US/US/web/-/track/{key}"
_SHAZAM_TRACK = re.compile(r"/track/(\d+)")
_SHAZAM_SONG = re.compile(r"/song/(\d+)")
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


@dataclass
class ShazamTrack:
    artist: str
    title: str
    genre: str | None
    year: int | None

    @property
    def query(self) -> str:
        return f"{self.artist} {self.title}".strip()


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _expand_short_link(url: str) -> str:
    """Follow a shz.am-style redirect to the real shazam.com URL."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.geturl()
    except urllib.error.HTTPError as exc:  # the 405 wall still reports where we landed
        return exc.geturl() or url
    except Exception as exc:
        log.warning("Could not expand Shazam link %s: %s", url, exc)
        return url


def resolve_shazam(url: str) -> ShazamTrack | None:
    """
    Turn a Shazam link into artist/title (plus Apple's genre and release
    year, used for the genre folder). Shazam is a song ID service, not an
    audio source, so music-dl then finds the track on YouTube.

    Link shapes: shazam.com/track/<shazam key>/<slug> (the app's share link,
    resolved through the discovery API), shazam.com/song/<apple id>/<slug>
    (an Apple Music id, resolved through the iTunes lookup API), optionally
    with a locale prefix; short links are expanded first.
    """
    if not (_SHAZAM_TRACK.search(url) or _SHAZAM_SONG.search(url)):
        url = _expand_short_link(url)

    if m := _SHAZAM_TRACK.search(url):
        try:
            data = _fetch_json(SHAZAM_TRACK_API.format(key=m.group(1)))
        except Exception as exc:
            log.warning("Shazam track API failed for %s: %s", url, exc)
            return None
        if not data.get("title"):
            return None
        released = next(
            (
                meta.get("text")
                for section in data.get("sections", [])
                for meta in section.get("metadata") or []
                if meta.get("title") == "Released"
            ),
            data.get("releasedate"),
        )
        return ShazamTrack(
            artist=data.get("subtitle", ""),
            title=data["title"],
            genre=(data.get("genres") or {}).get("primary"),
            year=library.year_of(released),
        )

    if m := _SHAZAM_SONG.search(url):
        track = library.itunes_lookup(m.group(1))
        if not track:
            return None
        return ShazamTrack(
            artist=track.get("artistName", ""),
            title=track.get("trackName", ""),
            genre=track.get("primaryGenreName"),
            year=library.year_of(track.get("releaseDate")),
        )

    log.warning("Unrecognized Shazam link: %s", url)
    return None


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
# Once-a-day dependency updates (yt-dlp / spotdl)
# --------------------------------------------------------------------------- #

# Written after today's check so later downloads skip brew/pip. Gitignored.
DEPS_STAMP = Path(__file__).resolve().parent / ".deps-checked"
DEPS_UPDATE_TIMEOUT = int(os.environ.get("DEPS_UPDATE_TIMEOUT", "300"))
_deps_lock: asyncio.Lock | None = None


def _deps_checked_today() -> bool:
    try:
        return DEPS_STAMP.read_text(encoding="utf-8").strip() == date.today().isoformat()
    except OSError:
        return False


def _mark_deps_checked() -> None:
    try:
        DEPS_STAMP.write_text(date.today().isoformat() + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("Could not write %s: %s", DEPS_STAMP, exc)


def _run_cmd(
    argv: list[str],
    timeout: int,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return -1, f"not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode if proc.returncode is not None else -1, text


def _cmd_version(argv: list[str]) -> str:
    rc, out = _run_cmd(argv, timeout=30)
    if rc != 0:
        return ""
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line.split()[0]
    return ""


def _brew_bin() -> str:
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if os.path.isfile(candidate):
            return candidate
    return "brew"


def _homebrew_ytdlp() -> str:
    brew = _brew_bin()
    prefix = str(Path(brew).parent)
    for candidate in (f"{prefix}/yt-dlp", "/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp"):
        if os.path.isfile(candidate):
            return candidate
    return "yt-dlp"


def _upgrade_download_deps() -> str:
    """
    Upgrade Homebrew yt-dlp plus the venv's yt-dlp and spotdl.
    Returns a short Telegram-ready summary. Always stamps today's date so we
    only do this once per day even if a command fails.
    """
    notes: list[str] = []
    brew = _brew_bin()
    brew_ytdlp = _homebrew_ytdlp()
    before_brew = _cmd_version([brew_ytdlp, "--version"])
    rc, out = _run_cmd(
        [brew, "upgrade", "yt-dlp"],
        timeout=DEPS_UPDATE_TIMEOUT,
        extra_env={"HOMEBREW_NO_ENV_HINTS": "1"},
    )
    after_brew = _cmd_version([brew_ytdlp, "--version"]) or before_brew
    if rc != 0:
        tail = out.splitlines()[-1] if out else "unknown error"
        notes.append(f"Homebrew yt-dlp update failed ({tail})")
        log.warning("brew upgrade yt-dlp failed rc=%s\n%s", rc, out[-1500:])
    elif before_brew and after_brew and before_brew != after_brew:
        notes.append(f"Homebrew yt-dlp {before_brew} → {after_brew}")
        log.info("Upgraded Homebrew yt-dlp %s -> %s", before_brew, after_brew)
    else:
        notes.append(f"Homebrew yt-dlp {after_brew or '?'} (current)")

    before_venv = _cmd_version(
        [sys.executable, "-c", "import yt_dlp; print(yt_dlp.version.__version__)"]
    )
    rc, out = _run_cmd(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-U",
            "--disable-pip-version-check",
            "--index-url",
            "https://pypi.org/simple",
            "yt-dlp",
            "spotdl",
        ],
        timeout=DEPS_UPDATE_TIMEOUT,
    )
    after_venv = _cmd_version(
        [sys.executable, "-c", "import yt_dlp; print(yt_dlp.version.__version__)"]
    ) or before_venv
    if rc != 0:
        tail = out.splitlines()[-1] if out else "unknown error"
        notes.append(f"venv yt-dlp/spotdl update failed ({tail})")
        log.warning("pip install -U yt-dlp spotdl failed rc=%s\n%s", rc, out[-1500:])
    elif before_venv and after_venv and before_venv != after_venv:
        notes.append(f"venv yt-dlp {before_venv} → {after_venv}")
        log.info("Upgraded venv yt-dlp %s -> %s", before_venv, after_venv)
    else:
        notes.append(f"venv yt-dlp {after_venv or '?'} (current)")

    _mark_deps_checked()
    return "🔧 Daily tool check:\n" + "\n".join(notes)


async def maybe_refresh_deps() -> str | None:
    """
    On the first download of the day, upgrade yt-dlp / spotdl.
    Returns a status line to send in Telegram, or None if already checked today.
    """
    global _deps_lock
    if _deps_lock is None:
        _deps_lock = asyncio.Lock()
    async with _deps_lock:
        if _deps_checked_today():
            return None
        log.info("Daily dependency check starting")
        return await asyncio.to_thread(_upgrade_download_deps)


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
    "Every song goes in *All Songs* plus a genre + decade folder "
    "(e.g. *Dembow 2020s*). Tap ✏️ under a download to fix its genre.\n\n"
    "Commands:\n"
    "/start, /help — this message\n"
    "/unknown — sort songs whose genre I couldn't figure out\n"
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


@dataclass
class JobResult:
    reply: str
    saved: list[str]                      # filenames now in All Songs/
    shazam: ShazamTrack | None = None     # genre/year hints for categorizing


async def _music_dl_job(arg: str, music_dir: str) -> JobResult:
    """Run music-dl for a URL or search phrase and summarize what it saved."""
    try:
        before = audio_names(songs_dir(music_dir))
        rc, out, err = await run_music_dl(arg, music_dir)
    except FileNotFoundError:
        return JobResult(
            "❌ Could not run music-dl.\n"
            f"MUSIC_DL is set to: {MUSIC_DL or '(unset)'}\n"
            "Check that the path is correct and the file is executable.",
            [],
        )
    log_job_output("music-dl", rc, out, err)
    found = new_audio_files(before, songs_dir(music_dir))
    return JobResult(*summarize_result(rc, out, err, found, music_dir))


async def process_job(kind: str, value: str, music_dir: str) -> JobResult:
    """Run one download job and return the reply + saved files. Never raises."""
    try:
        if kind == "spotify":
            try:
                before = audio_names(songs_dir(music_dir))
                rc, out, err = await run_spotdl(value, music_dir)
            except FileNotFoundError:
                return JobResult(
                    "❌ Spotify links need spotdl, which isn't installed.\n"
                    "Install it in the bot's venv:\n"
                    "  <venv>/bin/python3 -m pip install spotdl",
                    [],
                )
            log_job_output("spotdl", rc, out, err)
            found = new_audio_files(before, songs_dir(music_dir))
            return JobResult(*summarize_spotdl(rc, out, err, found, music_dir))

        if kind == "shazam":
            track = await asyncio.to_thread(resolve_shazam, value)
            if not track:
                return JobResult(
                    "❌ Couldn't read that Shazam link. Try opening it and "
                    "sending me the song name instead.",
                    [],
                )
            result = await _music_dl_job(track.query, music_dir)
            result.reply = f"🎧 Shazam → {track.artist} - {track.title}\n{result.reply}"
            result.shazam = track
            return result

        # "direct" (YouTube/SoundCloud/etc.) or "search" — both go to music-dl.
        return await _music_dl_job(value, music_dir)

    except Exception as exc:  # defensive: never let a job crash the handler
        log.exception("Unexpected error in job %s(%r)", kind, value)
        return JobResult(f"❌ Unexpected error: {exc}", [])


# --------------------------------------------------------------------------- #
# Genre tags + correction buttons
# --------------------------------------------------------------------------- #
#
# Callback data is "action|file id|genre idx|decade idx|mode" (Telegram caps
# it at 64 bytes, so files are referenced by library.file_id()):
#   g  open the genre picker        s  genre picked -> decade picker
#   d  decade picked -> save        x  cancel, back to the ✏️ button
#   k  skip this song (/unknown queue only)
# mode "q" marks the /unknown queue, which moves on to the next song after a
# choice instead of just updating the message.

TAG_PREFIX = "🏷 "
NOT_SURE = "n"


def _cb(action: str, fid: str, gi: int | str = "", di: int | str = "", mode: str = "") -> str:
    return f"{action}|{fid}|{gi}|{di}|{mode}"


def tag_line(cat: library.Category) -> str:
    if cat.genre == library.UNKNOWN:
        return f"{TAG_PREFIX}Genre: Unknown — which is it?"
    return f"{TAG_PREFIX}{cat.label}"


def change_button(fid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Change genre", callback_data=_cb("g", fid))]])


def genre_picker(fid: str, mode: str = "") -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(genre, callback_data=_cb("s", fid, i, mode=mode))
        for i, genre in enumerate(library.GENRES)
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    last = ("⏭ Skip", _cb("k", fid, mode=mode)) if mode == "q" else ("Cancel", _cb("x", fid))
    rows.append([InlineKeyboardButton(last[0], callback_data=last[1])])
    return InlineKeyboardMarkup(rows)


def decade_picker(fid: str, gi: int, mode: str = "") -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(decade, callback_data=_cb("d", fid, gi, i, mode))
        for i, decade in enumerate(library.DECADES)
    ]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([
        InlineKeyboardButton("Not sure", callback_data=_cb("d", fid, gi, NOT_SURE, mode)),
        InlineKeyboardButton("« Back", callback_data=_cb("g", fid, mode=mode)),
    ])
    return InlineKeyboardMarkup(rows)


def tag_markup(fid: str, cat: library.Category) -> InlineKeyboardMarkup:
    """Unknown songs open straight onto the picker; known ones get a ✏️ button."""
    return genre_picker(fid) if cat.genre == library.UNKNOWN else change_button(fid)


def _with_tag(text: str, cat: library.Category) -> str:
    """Replace the 🏷 line of a reply (or append one)."""
    lines = [ln for ln in text.splitlines() if not ln.startswith(TAG_PREFIX)]
    return "\n".join(lines + [tag_line(cat)])


async def send_tagged(message, music_dir: str, result: JobResult, prefix: str) -> None:
    """Categorize each saved file, then reply with its genre + correction buttons."""
    if not result.saved:
        await message.reply_text((prefix + result.reply)[:MAX_TG_MESSAGE])
        return

    hints = {}
    if result.shazam:
        hints = {"hint_genre": result.shazam.genre, "hint_year": result.shazam.year}
    cats = []
    for name in result.saved:
        try:
            cats.append(await asyncio.to_thread(library.categorize_and_place, Path(music_dir), name, **hints))
        except Exception:
            log.exception("Categorizing %s failed", name)
            cats.append(None)

    if len(result.saved) == 1 and cats[0]:
        text = _with_tag(prefix + result.reply, cats[0])
        fid = library.file_id(result.saved[0])
        await message.reply_text(text[:MAX_TG_MESSAGE], reply_markup=tag_markup(fid, cats[0]))
        return

    await message.reply_text((prefix + result.reply)[:MAX_TG_MESSAGE])
    for name, cat in zip(result.saved, cats):
        if cat:
            fid = library.file_id(name)
            await message.reply_text(f"🎵 {name}\n{tag_line(cat)}", reply_markup=tag_markup(fid, cat))


async def send_next_unknown(bot, chat_id: int, music_dir: str, skipped: set[str]) -> None:
    """Post the next Unknown song of this library with the genre picker."""
    pending = [n for n in await asyncio.to_thread(library.unknown_names, Path(music_dir)) if n not in skipped]
    if not pending:
        await bot.send_message(chat_id, "🎉 No songs left with an unknown genre.")
        return
    name = pending[0]
    prior = library.stored(Path(music_dir), name)
    hint = f"\nBest guess: {prior.detail}" if prior and prior.detail else ""
    await bot.send_message(
        chat_id,
        f"❓ {len(pending)} left\n🎵 {name}{hint}\nWhich genre is it?",
        reply_markup=genre_picker(library.file_id(name), mode="q"),
    )


async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/unknown — walk through this user's songs filed under Unknown/."""
    if not is_authorized(update):
        await deny(update)
        return
    ctx.chat_data["skipped"] = set()
    user_id = update.effective_user.id if update.effective_user else None
    await send_next_unknown(
        ctx.bot, update.effective_chat.id, music_dir_for_user(user_id), ctx.chat_data["skipped"]
    )


async def handle_genre_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Genre/decade picker buttons. Each user can only re-file songs in their own library."""
    query = update.callback_query
    if not is_authorized(update):
        await query.answer("Not authorized.", show_alert=True)
        return
    action, fid, gi, di, mode = (query.data.split("|") + [""] * 5)[:5]
    music_dir = music_dir_for_user(update.effective_user.id)
    lib = Path(music_dir)
    name = await asyncio.to_thread(library.name_for_id, lib, fid)
    if not name:
        await query.answer("That song isn't in your library anymore.", show_alert=True)
        return
    await query.answer()

    # Messages older than 48h arrive as InaccessibleMessage: no text, no edit
    # helpers. Fall back to a rebuilt text and edit by chat/message id.
    chat_id, message_id = query.message.chat.id, query.message.message_id
    current = library.stored(lib, name) or library.Category()
    text = getattr(query.message, "text", None) or f"🎵 {name}\n{tag_line(current)}"
    skipped = ctx.chat_data.setdefault("skipped", set())

    async def edit(new_text: str | None, markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            if new_text is None:
                await ctx.bot.edit_message_reply_markup(chat_id, message_id, reply_markup=markup)
            else:
                await ctx.bot.edit_message_text(new_text, chat_id, message_id, reply_markup=markup)
        except BadRequest as exc:  # double taps: "message is not modified"
            log.info("Edit skipped: %s", exc)

    if action == "g":
        await edit(None, genre_picker(fid, mode))
    elif action == "s":
        # The last line is the 🏷 tag (or the /unknown question): swap in the prompt.
        genre = library.GENRES[int(gi)]
        await edit(text.rsplit("\n", 1)[0] + f"\n{TAG_PREFIX}{genre} · which decade?", decade_picker(fid, int(gi), mode))
    elif action == "x":
        await edit(_with_tag(text, current), tag_markup(fid, current))
    elif action == "k":
        skipped.add(name)
        await edit(f"⏭ Skipped {name}")
        await send_next_unknown(ctx.bot, chat_id, music_dir, skipped)
    elif action == "d":
        genre = library.GENRES[int(gi)]
        decade = None if di == NOT_SURE else library.DECADES[int(di)]
        cat = await asyncio.to_thread(library.set_user_choice, lib, name, genre, decade)
        log.info("User %s filed %s under %s", update.effective_user.id, name, cat.folder)
        if mode == "q":
            await edit(f"✔️ {name}\n→ {cat.folder}/")
            await send_next_unknown(ctx.bot, chat_id, music_dir, skipped)
        else:
            await edit(_with_tag(text, cat) + " ✔️", change_button(fid))


_library_locks: dict[str, asyncio.Lock] = {}


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

    user_id = update.effective_user.id if update.effective_user else None
    music_dir = music_dir_for_user(user_id)
    log.info("Download for user id=%s -> %s", user_id, music_dir)

    # First download of the calendar day: upgrade yt-dlp (Homebrew + venv) and
    # spotdl so YouTube extractor breakage does not sit stale for weeks.
    if not _deps_checked_today():
        await message.reply_text("🔧 Checking yt-dlp / spotdl for updates (once a day)…")
        await ctx.bot.send_chat_action(message.chat_id, ChatAction.TYPING)
        deps_note = await maybe_refresh_deps()
        if deps_note:
            await message.reply_text(deps_note[:MAX_TG_MESSAGE])

    # Immediate acknowledgment so the user knows we're on it.
    ack = "⬇️ Downloading…" if len(jobs) == 1 else f"⬇️ Downloading {len(jobs)} items…"
    await message.reply_text(ack)
    await ctx.bot.send_chat_action(message.chat_id, ChatAction.TYPING)

    # One download at a time per library: music-dl's exit trap wipes the
    # shared All Songs/.tmp, which would kill a concurrent run's partial file.
    async with _library_locks.setdefault(music_dir, asyncio.Lock()):
        for i, (kind, value) in enumerate(jobs, start=1):
            prefix = f"[{i}/{len(jobs)}] " if len(jobs) > 1 else ""
            result = await process_job(kind, value, music_dir)
            await send_tagged(message, music_dir, result, prefix)


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
    if DALIZAS_USER_ID is not None and not DALIZAS_MUSIC_DIR:
        log.warning(
            "DALIZAS_USER_ID is set but DALIZAS_MUSIC_DIR is empty; "
            "that user will download into MUSIC_DIR."
        )
    if CLAIRE_USER_ID is not None and not CLAIRE_MUSIC_DIR:
        log.warning(
            "CLAIRE_USER_ID is set but CLAIRE_MUSIC_DIR is empty; "
            "that user will download into MUSIC_DIR."
        )
    if problems:
        for p in problems:
            log.error("CONFIG ERROR: %s", p)
        raise SystemExit(1)


def main() -> None:
    _validate_config()

    log.info("Starting music bot. Authorized user IDs: %s", sorted(ALLOWED_USER_IDS))
    log.info("Default library (MUSIC_DIR): %s", music_output_dir())
    if DALIZAS_USER_ID is not None and DALIZAS_MUSIC_DIR:
        log.info(
            "User %s library (DALIZAS_MUSIC_DIR): %s",
            DALIZAS_USER_ID,
            DALIZAS_MUSIC_DIR,
        )
    if CLAIRE_USER_ID is not None and CLAIRE_MUSIC_DIR:
        log.info(
            "User %s library (CLAIRE_MUSIC_DIR): %s",
            CLAIRE_USER_ID,
            CLAIRE_MUSIC_DIR,
        )
    app =Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("unknown", cmd_unknown))
    app.add_handler(CallbackQueryHandler(handle_genre_button))
    # block=False: a download can run for minutes; genre buttons and commands
    # must stay responsive meanwhile (handle_message serializes per library).
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message, block=False))

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
