# music-dl

Grab audio from a YouTube link (lyric videos, official videos, whatever) and drop
a tagged 320 kbps MP3 into [`Music/`](Music).

## Everyday use

Open Terminal, then:

```bash
cd ~/DJ && ./music-dl
```

You get a prompt. Paste a YouTube URL, press enter, wait a few seconds. Repeat as
many times as you want. Type `q` when you're done.

```
music-dl - YouTube to MP3, saved in /Users/orlandocedeno/DJ/Music
Paste a URL and press enter. Or type a song name to search for it.
Commands: q = quit, ls = recent downloads, open = open the folder

url> https://www.youtube.com/watch?v=kJQP7kiw5Fk
-> https://www.youtube.com/watch?v=kJQP7kiw5Fk
[download] 100% of 4.21MiB
* Luis Fonsi - Despacito ft. Daddy Yankee.mp3

url> q
```

At the `url>` prompt you can also type:

| Input | What happens |
|---|---|
| a YouTube URL | downloads exactly that video |
| several URLs on one line, space separated | downloads all of them |
| a song name, e.g. `sza snooze` | finds the **lyric video** and downloads that |
| `ls` | shows the 10 most recent files in `Music/` |
| `open` | opens `Music/` in Finder |
| `q` | quit (Ctrl-D works too) |

## Lyric videos

Typing a song name searches for the lyric version rather than the official music
video. It scans the top 15 results and takes the first one whose title says
"lyric":

```
url> sza snooze
-> searching YouTube for a lyric video of "sza snooze"
   found https://www.youtube.com/watch?v=CBx6e9cZlBQ
* SZA - Snooze.mp3
```

If nothing in the results is a lyric video — instrumentals, game soundtracks, B-sides —
it says so and falls back to the top regular result rather than failing.

**Pasting a URL always downloads that exact video.** The script never swaps your
link for a different one behind your back. So if you have a music-video URL and
want the lyric version instead, type the song name rather than pasting the link.

Use `--any` to search normally without the lyric preference:

```bash
./music-dl --any "daft punk around the world"
```

One caveat worth knowing: lyric videos are usually fan re-uploads rather than the
artist's own channel. Most sound fine, but some are sourced from a lower-quality
rip, and a few are slightly pitched or sped up to dodge copyright detection —
which matters if you're beatmatching. If a track sounds off, re-grab it with
`--any` or paste the official URL directly.

## One-liner use

Skip the prompt entirely and pass URLs as arguments:

```bash
./music-dl "https://www.youtube.com/watch?v=kJQP7kiw5Fk"
```

Several at once:

```bash
./music-dl "https://youtu.be/AAA" "https://youtu.be/BBB" "https://youtu.be/CCC"
```

## What you get

- **320 kbps MP3** in `Music/`
- **Cover art** embedded from the video thumbnail
- **Real ID3 tags** — artist, title, year — so DJ software (rekordbox, Serato,
  Traktor) sorts it properly instead of showing one giant blob of a filename
- **Cleaned-up names.** `Bad Bunny - Me Porto Bonito (Official Lyric Video)`
  becomes `Bad Bunny - Me Porto Bonito.mp3`. Junk like `(Official Video)`,
  `[Lyrics]`, `(Audio)`, `[4K]`, `- Topic`, `VEVO` gets stripped.
- **No duplicates.** Every video ID you download is logged in
  `.download-archive.txt`. Paste the same link again and it skips instantly.

## Flags

```bash
./music-dl --any "song name"    # search normally, don't prefer lyric videos
./music-dl --raw <url>          # keep the exact YouTube title, no cleanup
./music-dl --quality 192 <url>  # smaller files (default is 320)
./music-dl --playlist <url>     # download the entire playlist, not just one video
./music-dl --help
```

By default a link that points into a playlist grabs only that one video, which is
almost always what you want.

## First-time setup

Already done on this machine, but if you move to a new Mac:

```bash
brew install yt-dlp ffmpeg
```

## Troubleshooting

**"Sign in to confirm you're not a bot"** or downloads suddenly failing —
YouTube changed something. Update the downloader first, this fixes it ~90% of the
time:

```bash
brew upgrade yt-dlp
```

**A song came out with a weird name.** Just rename the file in Finder. Or
re-download it with `--raw` to see the untouched title.

**Wrong artist tag.** The artist is read from the video title when it's formatted
`Artist - Song`, otherwise it falls back to the channel name. Fix it in your DJ
software's tag editor, or in Music.app.

**It re-downloaded something I already had.** Songs already in `Music/` from
before this script existed aren't in `.download-archive.txt`, so the first time
you re-grab one it won't know. It will after that.

## Fixing up older files: `refresh-library`

Your original 40 MP3s had no cover art, no tags, and 108–192 kbps audio. This
script backfills all of that.

```bash
./refresh-library          # dry run — shows what it would search for
./refresh-library --go     # actually do it
./refresh-library --report # table of what happened
```

Not happy with how one track came out? Redo just that one:

```bash
./refresh-library --go "Ghetto Love Story"
```

**It never touches `Music/`.** Results are written to `Music-refreshed/` so you
can listen before replacing anything.

For each file it searches YouTube *and* SoundCloud, then only accepts a result
whose **length matches your original within 4 seconds**. That check is the whole
point — without it "NOLA Bounce Remix" comes back as a 20-minute DJ mix. It also
rejects live, cover, karaoke, sped-up, and slowed versions unless your filename
asked for one.

Each file ends up in one of three states:

| Result | What happened |
|---|---|
| `UPGRADED` | real match found — fresh 320 kbps audio, cover art, artist/title tags |
| `ART-ONLY` | no confident match, so **your exact audio was kept** and art was added to it |
| `UNCHANGED` | couldn't find anything, original copied through as-is |

So a track can never come out worse than it went in. Worst case it keeps the
audio it already had and gains an image.

Once you've listened and you're happy:

```bash
mv Music Music-old && mv Music-refreshed Music
```

Keep `Music-old` around until you're sure.

## Files

| Path | What it is |
|---|---|
| [`music-dl`](music-dl) | the main downloader |
| [`refresh-library`](refresh-library) | bulk art/tag backfill for files you already had |
| [`Music/`](Music) | your library, where every MP3 lands |
| `Music-refreshed/` | output of `refresh-library` — review, then swap in |
| `.download-archive.txt` | list of already-downloaded video IDs (delete to reset dedup) |
| `refresh-report.tsv` | per-file record of the last refresh run |
| [`CLAUDE.md`](CLAUDE.md) | notes for a future AI coding session |

## A note on what you download

This pulls audio off YouTube. Keep it to material you have the right to use —
your own uploads, Creative Commons tracks, promo pools, or personal-use copies of
music you own. Don't distribute the output.
