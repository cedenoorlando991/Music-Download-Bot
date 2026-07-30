# DJ — YouTube to MP3 library

Personal DJ music library plus the tool that fills it. Not a git repo.

## Layout

```
DJ/
├── music-dl                 # the main tool: one bash script, no deps of its own
├── refresh-library          # one-off bulk backfill of art/tags onto existing files
├── Music/                   # the library — every MP3 lands here (note: capital M)
├── Music-refreshed/         # output of refresh-library; NOT the live library
├── .download-archive.txt    # yt-dlp archive of downloaded video IDs, for dedup
├── refresh-report.tsv       # last refresh-library run, one row per file
├── README.md                # user-facing instructions
└── CLAUDE.md                # this file
```

## What `music-dl` does

Wraps `yt-dlp` + `ffmpeg`. Takes a YouTube URL (or a search phrase), extracts
bestaudio, transcodes to 320 kbps MP3, embeds the thumbnail as cover art, writes
ID3 artist/title/date tags, and moves the result into `Music/`.

Two modes, both in the same script:

- **Interactive** (no args) — a `url>` read loop. This is the primary way the
  user runs it. Also accepts `ls`, `open`, `help`, `q`.
- **One-shot** (args) — each argument is a URL or a search phrase.

## Lyric-video preference

The user specifically wants lyric videos over official music videos. A search
phrase (not a URL) triggers a pre-pass: `--flat-playlist` over `ytsearch15:` with
`--match-filter "title ~= '(?i)lyric'"`, `--print %(webpage_url)s`, take the first
hit. Empty result falls back to plain `ytsearch1:`. `--any` (`LYRICS=0`) skips the
pre-pass entirely.

**A pasted URL is always downloaded verbatim.** Do not add logic that substitutes
a different video for a URL the user supplied — silently swapping their link for a
"better" match is exactly the kind of surprise this tool should not have. The
documented workaround is to type the song name instead of pasting the URL.

Note the pre-pass costs an extra network round trip (~1-2s) on search input only.

## Requirements

`yt-dlp` and `ffmpeg`, both from Homebrew (`/opt/homebrew/bin`). The script checks
for them on startup and prints an install hint if either is missing.

## Constraints to respect when editing

- **macOS ships bash 3.2.** `/usr/bin/env bash` resolves to 3.2.57. No `${var,,}`,
  no associative arrays, no `mapfile`, no `&>>`. Use `tr` for case folding.
  This has already bitten this script once.
- **`set -u` is deliberately off.** Under bash 3.2, `${#arr[@]}` on an empty array
  errors with `-u`. Don't add it back without rewriting the array handling.
- **`--ignore-config` is deliberate.** Keeps a stray `~/.config/yt-dlp/config`
  from silently changing output paths or formats.
- Everything writes through `--paths` / `--paths temp:`, so partial downloads land
  in `Music/.tmp/` and never leave junk in the library on failure.

## How the naming/tagging pipeline works

Order matters — these are all yt-dlp postprocessors applied in command-line order
inside `build_args()`:

1. `--replace-in-metadata title` strips noise: bracketed junk like
   `(Official Lyric Video)` / `[4K]`, then a trailing unbracketed tag like
   `- Official Audio`, then whitespace collapse. Match list is the `$JUNK` var.
2. `--replace-in-metadata uploader,artist,creator` strips `- Topic` and `VEVO`.
3. `--parse-metadata "title:(?P<artist>[^-]+?) - (?P<title>.+)"` splits
   `Artist - Song` into separate fields. Non-matching titles pass through
   untouched.
4. `--parse-metadata "%(artist,uploader)s:(?P<meta_artist>.+)"` sets the ID3
   artist tag with a channel-name fallback. It writes `meta_artist`, **not**
   `artist`, on purpose: `artist` drives the output filename, so overwriting it
   would put the channel name into every filename.
5. Four `--parse-metadata ":(?P<meta_*>)"` lines blank out
   comment/description/synopsis/purl — otherwise the entire YouTube description
   gets embedded in the MP3.

Output template is `%(artist&{} - |)s%(title)s.%(ext)s` — the `&`/`|` syntax emits
`Artist - ` only when an artist was actually parsed, so titles without a `-` come
out as just `Song.mp3` rather than `NA - Song.mp3`.

`--raw` (`CLEAN=0`) skips steps 1 and 2 only.

## Testing changes

Never test into `Music/` — it's the user's real library. Override both paths:

```bash
MUSIC_DIR=/tmp/testmusic ARCHIVE=/tmp/test-arch.txt ./music-dl "<url>"
```

Verify the result with `ffprobe`, not just the filename:

```bash
ffprobe -v error -show_entries format_tags=title,artist,date -of default=nw=1 file.mp3
ffprobe -v error -show_entries stream=codec_name,bit_rate -of csv=p=0 file.mp3
```

Worth covering after any change to `build_args()`:

| Case | Expect |
|---|---|
| title `Artist - Song (Official Video)` | `Artist - Song.mp3`, artist + title tags split |
| title with no `-` | `Song.mp3`, artist tag falls back to channel |
| same URL twice | second run prints "already in your library, skipped" |
| dead video ID | prints `x failed:`, exits 1, leaves no `.tmp` behind |
| bare search phrase | resolves to a video whose title contains "lyric" |
| search phrase with no lyric video anywhere in results | prints "no lyric video found", still downloads |
| `--any "phrase"` | skips the lyric pre-pass, plain `ytsearch1:` |
| pasted URL | downloads that exact video, no substitution |

## `refresh-library`

Written to backfill cover art onto the ~40 pre-existing MP3s, which had no art,
no ID3 tags at all, and 108-192 kbps audio. Mostly a one-off, but it is safe to
re-run — it skips anything that already has art.

The user's originals came from mixed sources (some SoundCloud, several NOLA
bounce / jersey club edits) and no source URLs were recorded, so matching has to
go through search. The whole design is about not silently swapping a track for
the wrong version:

1. Derive a query from the filename (`to_query`).
2. Gather candidates from `ytsearch10:<q> lyrics`, `ytsearch10:<q>`, and
   `scsearch10:<q>` — SoundCloud matters here, it's the only place some of the
   bounce edits exist.
3. `pick_match` keeps only candidates within `TOLERANCE` (4s) of the original's
   duration, rejects live/cover/karaoke/sped-up/slowed titles unless the original
   filename asked for them, and prefers lyric videos on ties.
4. Match found → download it via `music-dl`. No match → **keep the user's audio
   byte-for-byte** and only embed cover art via `embed_art` (`ffmpeg -c:a copy`).

Two invariants, both load-bearing:

- **`Music/` is never written to.** Everything goes to `Music-refreshed/`. The
  swap into the live library is the user's call, not the script's.
- **Audio is never downgraded.** Worst case a file keeps exactly the audio it had
  and gains an image.

Re-run one track without touching the rest:

```bash
./refresh-library --go "Ghetto Love Story"
```

The filter is a substring match on the original filename. A filtered run preserves
the other rows in `refresh-report.tsv` rather than truncating it.

The duration check is the safety net. Without it, "NOLA Bounce Remix" resolves to
a 20-minute DJ mix and "Decode" resolved to a live version — both observed during
development.

### Two bugs already paid for — don't reintroduce them

**Never use `|` as a field delimiter for `yt-dlp --print`.** YouTube titles
routinely contain it (`Artist - Song | From The Block Performance`). The original
`--print "%(duration)s|%(title)s|%(webpage_url)s"` shifted fields on those titles,
so awk's `$3` was a fragment of the title instead of a URL. That fragment got
passed to `music-dl`, which treated it as a search phrase and downloaded a
completely unrelated song. It hit 2 of 41 files and both looked like clean
successes in the log. The format is now tab-separated with the **URL before the
title**, and `pick_match` rejects any field-2 value that isn't `^https?://`.

**Always re-verify the downloaded file, not the search metadata.** `pick_match`
filters on the duration the search *advertises*; the loop now re-runs `dur()` on
the actual downloaded MP3 and discards it (falling back to art-only) if it's out
of tolerance. The delimiter bug is exactly the class of failure that a
metadata-only check cannot see.

The general lesson: the per-file `OLD`/`NEW` duration columns in the report are
what surfaced both bugs. Keep logging real measured values, not intended ones.

## Known rough edges

- MP3s that predate this script aren't in `.download-archive.txt`, so dedup
  doesn't know about them. `--no-overwrites` still prevents clobbering when the
  filename happens to match.
- The `$JUNK` regex is a heuristic. A song legitimately titled e.g.
  `... Video ...` would get mangled; `--raw` is the escape hatch.
- Lyric videos are typically fan re-uploads, so audio quality is less consistent
  than an official channel's, and some are pitched or time-stretched to evade
  Content ID. Relevant for beatmatching. `--any` or a pasted official URL avoids it.
- There is no duration cap. A vague search phrase can land on a multi-hour video
  (hit this during testing with a "test tone" query). Adding
  `--match-filter "duration < N"` would fix it but would also block legitimate
  long DJ mixes, so it was left out deliberately.
- YouTube extraction breaks periodically. Fix is `brew upgrade yt-dlp`, not a
  change to this script.
