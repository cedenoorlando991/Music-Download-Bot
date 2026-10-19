"""
library.py — genre + decade categorization for a music library folder.

Layout of every library (Music/, Dalizas Music/, claire!/):

    All Songs/           every real MP3 lives here (downloads land here)
    Dembow 2020s/        hard links into All Songs/, one folder per combo
    R&B 2000s/
    Unknown/             no confident match yet — the bot asks the user

Hard links mean a song shows up in both places with no extra disk space and
DJ software sees real files. Moving a song between genres only re-links it;
All Songs/ is never touched.

The genre for a file is decided in this order:
  1. A choice the user made in Telegram (sticky, stored in .genres.json).
  2. A keyword in the filename ("dembow", "bouyon", "jersey club", …).
  3. A hint from the source: Shazam reports Apple's genre + release year,
     and spotdl writes Spotify's artist genres into the ID3 genre tag.
  4. The iTunes Search API, then Deezer for what the US iTunes store lacks.
     Only candidates whose artist AND title match the file count; the decade
     is the earliest release year among them, so a 2015 compilation doesn't
     relabel a 1997 song.
  5. Otherwise Unknown.

Apple lumps dembow and reggaeton together as "Urbano latino", so Dominican
artists (DEMBOW_ARTISTS) tip that bucket to Dembow; likewise BACHATA_ARTISTS
pull bachata acts out of "Pop Latino".
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

log = logging.getLogger("music_bot.library")

ALL_SONGS = "All Songs"
UNKNOWN = "Unknown"
INDEX_NAME = ".genres.json"
AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".flac", ".wav", ".aac", ".ogg"}

GENRES = [
    "Dembow",
    "Reggaeton",
    "Bachata & Tropical",
    "Dancehall",
    "Bouyon",
    "Afrobeats & Amapiano",
    "Rap & Hip Hop",
    "R&B",
    "Jersey Club & Bounce",
    "Funk",
    "Pop",
    "Japanese Pop",
    "Rock",
]
DECADES = ["1960s", "1970s", "1980s", "1990s", "2000s", "2010s", "2020s"]

# Apple Music / iTunes genre names -> our genres. First match wins, so the
# specific rules sit above the broad ones ("latin rap" before "rap",
# "latin pop" before "latin").
_APPLE_GENRE_RULES = [
    (r"dembow", "Dembow"),
    (r"jersey|bounce", "Jersey Club & Bounce"),
    (r"j-?pop|j-?rock|anime|kayokyoku|enka|japan", "Japanese Pop"),
    (r"pop latino|latin pop|k-?pop", "Pop"),
    (r"tropical|bachata|merengue|salsa|cumbia", "Bachata & Tropical"),
    (r"reggaeton|urbano|latin", "Reggaeton"),
    (r"dancehall|reggae", "Dancehall"),
    (r"bouyon", "Bouyon"),
    (r"afro|african|amapiano|highlife", "Afrobeats & Amapiano"),
    (r"brazil|brasil|carioca|baile", "Funk"),
    (r"hip-?hop|rap|drill|trap|dirty south|grime", "Rap & Hip Hop"),
    (r"r&b|soul|funk|disco|quiet storm", "R&B"),
    (r"rock|alternative|metal|punk|grunge|emo", "Rock"),
    (r"pop", "Pop"),
]

# Filename keywords that beat any lookup: these sub-genres are exactly the
# ones iTunes files under something broader (or doesn't carry at all).
_KEYWORD_RULES = [
    (r"\bdembow\b", "Dembow"),
    (r"\bbouyon\b", "Bouyon"),
    (r"jersey ?club|club (?:mix|remix)|bounce ?mix|bounce (?:remix|edit)|nola bounce", "Jersey Club & Bounce"),
    (r"amapiano", "Afrobeats & Amapiano"),
    (r"\bbachata\b", "Bachata & Tropical"),
    (r"baile funk|funk brasileiro|funk carioca|brazilian funk|montagem|\bmtg\b", "Funk"),
]

# Dominican urbano artists: an "Urbano latino" hit by any of them is dembow.
DEMBOW_ARTISTS = {
    "el alfa", "rochy rd", "tokischa", "chimbala", "bulin 47", "kiko el crazy",
    "musicologo", "mozart la para", "shelow shaq", "black jonas point",
    "haraca kiko", "secreto el famoso biberon", "lirico en la casa",
    "yailin la mas viral", "donaty", "la perversa", "braulio fogon",
    "ceky viciny", "dowba montana", "el mayor clasico", "melymel", "jey one",
    "quimico ultra mega", "bulova", "chael produciendo", "el cherry scom",
    "yomel el meloso", "la insuperable", "vakero", "flow 28", "arlene mc",
    "alofoke", "la mas doll", "onguito wa", "nfasis", "rochy", "kaly ocho",
    "liro shaq", "el fother", "jc la nevula", "harryson", "atomic otro way",
    "bigx thaphenom", "yaisel lm", "luis flow", "albert hdk", "el villano rd",
}

# Bachata artists Apple files under "Pop Latino" / "Latin" (Aventura's La Novelita).
BACHATA_ARTISTS = {
    "aventura", "romeo santos", "prince royce", "anthony santos", "frank reyes",
    "raulin rodriguez", "zacarias ferreira", "monchy and alexandra", "xtreme",
    "toby love", "elvis martinez", "hector acosta", "joe veras", "luis vargas",
    "grupo extra", "dalvin la melodia", "kiko rodriguez", "yoskar sarante",
    "luis miguel del amargue", "teodoro reyes", "joan soriano", "el chaval de la bachata",
    "henry santos", "lenny santos", "leslie grace", "karlos rose",
}

# Artist lists that override a broad Apple genre (Unknown, Pop, Latin/Reggaeton).
ARTIST_GENRES = [("Dembow", DEMBOW_ARTISTS), ("Bachata & Tropical", BACHATA_ARTISTS)]


@dataclass
class Category:
    genre: str = UNKNOWN
    decade: str | None = None
    source: str = "unknown"   # user | keyword | source | itunes | deezer | artist | unknown
    detail: str = ""          # what matched, for reports/logs

    @property
    def folder(self) -> str:
        return folder_name(self.genre, self.decade, user_set=self.source == "user")

    @property
    def label(self) -> str:
        if self.genre == UNKNOWN:
            return UNKNOWN
        return f"{self.genre} · {self.decade or 'decade unknown'}"


def folder_name(genre: str, decade: str | None, *, user_set: bool = False) -> str:
    """
    "Dembow 2020s". A guess missing either half goes to Unknown/ so the user is
    asked; a user who picked a genre but said "not sure" about the decade gets
    a decade-less "Dembow/" folder instead of being asked again forever.
    """
    if genre == UNKNOWN or genre not in GENRES:
        return UNKNOWN
    if decade:
        return f"{genre} {decade}"
    return genre if user_set else UNKNOWN


def decade_of(year: int | None) -> str | None:
    if not year or year < 1900:
        return None
    return f"{year // 10 * 10}s"


def file_id(name: str) -> str:
    """Short stable id for a filename, small enough for Telegram callback data."""
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]


# --------------------------------------------------------------------------- #
# Text normalization / matching
# --------------------------------------------------------------------------- #

def _norm(text: str) -> str:
    """Lowercase ASCII words only: drops accents, brackets, hashtags, upload junk
    ("Lyrics", "Official Video") and feat. credits."""
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", text)
    text = re.sub(r"#\w+", " ", text)  # "#jerseyclub"
    text = re.sub(r"\b(?:official|lyrics?|video|audio|visuali[sz]er|hd|hq|4k|mv|letra)\b", " ", text)
    text = re.split(r"\s(?:feat\.?|ft\.?|featuring|prod\.?|w/)\s", f" {text} ")[0]
    text = text.replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _split_artists(artist: str) -> list[str]:
    parts = re.split(r",|&|\+|\bx\b|\band\b|\bfeat\.?|\bft\.?|\bwith\b|/", artist, flags=re.IGNORECASE)
    return [n for n in (_norm(p) for p in parts) if n]


def _artist_match(ours: str, theirs: str) -> bool:
    theirs_n = f" {_norm(theirs)} "
    theirs_parts = _split_artists(theirs)
    for name in _split_artists(ours):
        if f" {name} " in theirs_n or any(f" {p} " in f" {name} " for p in theirs_parts):
            return True
    return False


def _title_match(ours: str, theirs: str) -> bool:
    a, b = _norm(ours), _norm(theirs)
    if not a or not b:
        return False
    if a == b:
        return True
    # "Diamonds & Pearls" vs "Diamonds & Pearls - dj blackpower flip"
    if len(a) >= 4 and len(b) >= 4 and (b.startswith(a) or a.startswith(b)):
        return True
    wa, wb = set(a.split()), set(b.split())
    return len(wa & wb) / len(wa | wb) >= 0.75


# "Artist - Title" separators seen in real filenames: " - ", "Jackson- Liberian",
# "Combo -Tu", em/en dashes, "Song ｜ Artist" (NFKC turns ｜ into |), "Song ·Artist".
# A hyphen with no space on either side (Jay-Z, Vishal-Shekhar) is not one.
_SEPARATOR = re.compile(r"\s+[-–—|·]\s*|\s*[-–—|·]\s+")


def parse_name(path: Path) -> tuple[str, str]:
    """
    (artist, title) for a library file, split on the first separator; any
    third part ("| From The Block Performance") is dropped. Falls back to the
    ID3 artist tag when the name has no separator. Names are sometimes
    "Title - Artist"; categorize() retries the lookup swapped.
    """
    stem = path.name
    while Path(stem).suffix.lower() in AUDIO_EXTS:  # "x.mp3.mp3" exists in the wild
        stem = Path(stem).stem
    parts = [p.strip() for p in _SEPARATOR.split(unicodedata.normalize("NFKC", stem)) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return (_read_tag(path, "artist") or "").strip(), stem.strip()


# --------------------------------------------------------------------------- #
# Genre mapping
# --------------------------------------------------------------------------- #

def map_apple_genre(apple_genre: str, artist: str = "") -> str:
    """Apple/iTunes/Shazam genre name -> one of GENRES (or UNKNOWN)."""
    low = (apple_genre or "").lower()
    genre = UNKNOWN
    for pattern, ours in _APPLE_GENRE_RULES:
        if re.search(pattern, low):
            genre = ours
            break
    if genre in (UNKNOWN, "Pop", "Reggaeton"):
        return artist_genre(artist) or genre
    return genre


def artist_genre(artist: str) -> str | None:
    """The genre of a known dembow/bachata artist in the credits, if any."""
    names = _split_artists(artist)
    return next((genre for genre, known in ARTIST_GENRES if any(n in known for n in names)), None)


def keyword_genre(name: str) -> str | None:
    # Raw name too: _norm drops bracketed text like "(Jersey Club Remix)".
    low = _norm(name) + " " + name.lower()
    for pattern, genre in _KEYWORD_RULES:
        if re.search(pattern, low):
            return genre
    return None


# --------------------------------------------------------------------------- #
# iTunes Search API (free, no key; ~20 requests/minute)
# --------------------------------------------------------------------------- #

_itunes_lock = threading.Lock()
_itunes_last = 0.0


def _itunes_get(url: str) -> dict:
    """GET an iTunes endpoint, paced under its rate limit, one retry on 403/429."""
    global _itunes_last
    for attempt in range(2):
        with _itunes_lock:
            interval = float(os.environ.get("ITUNES_MIN_INTERVAL", "3.1"))
            wait = _itunes_last + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _itunes_last = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and attempt == 0:
                log.warning("iTunes rate-limited (%s); backing off 60s", exc.code)
                time.sleep(60)
                continue
            raise
    raise AssertionError("unreachable")


def itunes_search(term: str, limit: int = 25) -> list[dict]:
    query = urllib.parse.urlencode({"term": term, "entity": "song", "limit": limit})
    try:
        return _itunes_get(f"https://itunes.apple.com/search?{query}").get("results", [])
    except Exception as exc:
        log.warning("iTunes search failed for %r: %s", term, exc)
        return []


def itunes_lookup(track_id: str) -> dict | None:
    """One track by Apple Music id (what shazam.com/song/<id> links carry)."""
    try:
        results = _itunes_get(f"https://itunes.apple.com/lookup?id={track_id}").get("results", [])
    except Exception as exc:
        log.warning("iTunes lookup failed for %s: %s", track_id, exc)
        return None
    return next((r for r in results if r.get("wrapperType") == "track"), None)


def year_of(release_date: str | None) -> int | None:
    m = re.search(r"\b(19|20)\d{2}\b", release_date or "")
    return int(m.group(0)) if m else None


def _matching(results: list[dict], artist: str, title: str, artist_key, title_key) -> list[dict]:
    """Results whose artist AND title match the file (exact title when there's no artist)."""
    hits = [
        r for r in results
        if _title_match(title, title_key(r)) and (not artist or _artist_match(artist, artist_key(r)))
    ]
    if not artist:
        hits = [r for r in hits if _norm(title_key(r)) == _norm(title)][:1]
    return hits


def _search_term(artist: str, title: str) -> str:
    names = _split_artists(artist)
    return f"{names[0] if names else ''} {_norm(title)}".strip()


def lookup_itunes(artist: str, title: str) -> Category:
    """
    Search iTunes and keep only results matching both artist and title. Genre
    is the most common mapped genre among them; decade is the earliest year.
    """
    hits = _matching(
        itunes_search(_search_term(artist, title)), artist, title,
        lambda r: r.get("artistName", ""), lambda r: r.get("trackName", ""),
    )
    if not hits:
        return Category()
    genres = Counter(
        g for g in (map_apple_genre(r.get("primaryGenreName", ""), r.get("artistName", "")) for r in hits)
        if g != UNKNOWN
    )
    years = sorted(y for y in (year_of(r.get("releaseDate")) for r in hits) if y)
    first = years[0] if years else None
    if len(years) > 1 and years[1] - years[0] > 5 and years.count(years[0]) == 1:
        # A lone early outlier is often a mis-dated compilation ("15 80's Songs"
        # dated 1973 for a 1981 single) — but can be the real original. Ask MusicBrainz.
        first = musicbrainz_first_year(artist, title) or first
    best = hits[0]
    detail = (
        f"iTunes: {best.get('artistName')} - {best.get('trackName')} "
        f"({best.get('primaryGenreName')}, {first or '?'})"
    )
    genre = genres.most_common(1)[0][0] if genres else UNKNOWN
    return Category(genre, decade_of(first), "itunes", detail)


def _deezer_get(path: str) -> dict:
    try:
        with urllib.request.urlopen(f"https://api.deezer.com/{path}", timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        log.warning("Deezer request failed for %s: %s", path, exc)
        return {}


def lookup_deezer(artist: str, title: str) -> Category:
    """
    Fallback for tracks the US iTunes store doesn't carry. Genre comes from the
    album (Deezer lists e.g. ["Pop", "Latin Music"]; the rule order in
    map_apple_genre picks the specific one). Album dates are often reissues,
    so the earliest of the first three matching albums is used.
    """
    query = urllib.parse.urlencode({"q": _search_term(artist, title), "limit": 15})
    hits = _matching(
        _deezer_get(f"search?{query}").get("data", []), artist, title,
        lambda r: (r.get("artist") or {}).get("name", ""), lambda r: r.get("title", ""),
    )
    album_ids = list(dict.fromkeys((r.get("album") or {}).get("id") for r in hits))[:3]
    albums = [a for a in (_deezer_get(f"album/{i}") for i in album_ids if i) if a]
    if not albums:
        return Category()
    genre_names = [g["name"] for g in (albums[0].get("genres") or {}).get("data", [])]
    years = [y for y in (year_of(a.get("release_date")) for a in albums) if y]
    best = hits[0]
    detail = (
        f"Deezer: {best['artist']['name']} - {best['title']} "
        f"({', '.join(genre_names) or '?'}, {min(years) if years else '?'})"
    )
    first = musicbrainz_first_year(artist, title)
    if first:
        years.append(first)
    genre = map_apple_genre(", ".join(genre_names), best["artist"]["name"])
    return Category(genre, decade_of(min(years) if years else None), "deezer", detail)


_mb_lock = threading.Lock()
_mb_last = 0.0


def musicbrainz_first_year(artist: str, title: str) -> int | None:
    """
    Earliest release year of a matching recording. Deezer's album dates are
    often reissues (2Pac's 1997 "Do For Love" shows as 2011); MusicBrainz
    tracks the original. Paced at 1 request/second per their usage policy.
    """
    global _mb_last
    names = _split_artists(artist)
    if not names:
        return None
    query = f'recording:"{_norm(title)}" AND artist:"{names[0]}"'
    url = "https://musicbrainz.org/ws/2/recording?" + urllib.parse.urlencode(
        {"query": query, "fmt": "json", "limit": 25}
    )
    with _mb_lock:
        wait = _mb_last + 1.1 - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _mb_last = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": "music-dl-categorizer/1.0 (personal use)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            recordings = json.loads(resp.read().decode("utf-8", "replace")).get("recordings", [])
    except Exception as exc:
        log.warning("MusicBrainz lookup failed for %r: %s", query, exc)
        return None
    years = [
        year_of(r.get("first-release-date"))
        for r in recordings
        if _title_match(title, r.get("title", ""))
        and _artist_match(artist, " & ".join(a.get("name", "") for a in r.get("artist-credit", [])))
    ]
    years = [y for y in years if y]
    return min(years) if years else None


# --------------------------------------------------------------------------- #
# Categorization
# --------------------------------------------------------------------------- #

def categorize(
    path: Path,
    *,
    hint_genre: str | None = None,
    hint_year: int | None = None,
    offline: bool = False,
) -> Category:
    """
    Best guess for one file (ignores any stored user choice — see resolve()).
    hint_genre/hint_year come from the download source (Shazam). offline=True
    skips the network lookup.
    """
    artist, title = parse_name(path)
    genre = keyword_genre(path.name)
    source = "keyword" if genre else "unknown"
    detail = ""
    decade = decade_of(hint_year)

    hint_genre = hint_genre or _read_tag(path, "genre")
    # A YouTube download's tag is just the video category ("Music"): not a hint.
    if hint_genre and (hint_genre in GENRES or map_apple_genre(hint_genre) != UNKNOWN):
        detail = f"Source: {hint_genre}, {hint_year or '?'}"
        if not genre:
            hinted = hint_genre if hint_genre in GENRES else map_apple_genre(hint_genre, artist)
            genre, source = hinted, "source"

    # Try "Artist - Title" first, then the same name read as "Title - Artist".
    pairs = [(artist, title), (title, artist)] if artist else [(artist, title)]
    if title and not offline:
        for (a, t), lookup in ((pair, lookup) for pair in pairs for lookup in (lookup_itunes, lookup_deezer)):
            if genre and decade:
                break
            found = lookup(a, t)
            if found.source == "unknown":
                continue
            if not genre and found.genre != UNKNOWN:
                genre, source = found.genre, found.source
            decade = decade or found.decade
            detail = detail or found.detail

    if not genre and artist_genre(artist):
        genre, source = artist_genre(artist), "artist"
    if genre and not decade:
        # Mostly edits/remixes (a keyword genre, nothing in iTunes/Deezer): music-dl
        # tagged the YouTube upload date, which is when the edit came out.
        uploaded = year_of(_read_tag(path, "date"))
        if uploaded:
            decade = decade_of(uploaded)
            detail = detail or f"decade from YouTube upload ({uploaded})"
    return Category(genre or UNKNOWN, decade, source, detail)


# --------------------------------------------------------------------------- #
# Index (.genres.json) + placement on disk
# --------------------------------------------------------------------------- #

_index_lock = threading.Lock()


def _index_path(lib: Path) -> Path:
    return Path(lib) / INDEX_NAME


def load_index(lib: Path) -> dict[str, dict]:
    try:
        return json.loads(_index_path(lib).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_index(lib: Path, index: dict[str, dict]) -> None:
    path = _index_path(lib)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")  # bot + categorize-library may both write
    tmp.write_text(json.dumps(index, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def stored(lib: Path, name: str) -> Category | None:
    entry = load_index(lib).get(name)
    if not entry:
        return None
    return Category(entry.get("genre", UNKNOWN), entry.get("decade"), entry.get("source", "unknown"), entry.get("detail", ""))


def name_for_id(lib: Path, fid: str) -> str | None:
    """Resolve a callback id back to a filename in All Songs/."""
    for entry in sorted((Path(lib) / ALL_SONGS).iterdir()):
        if file_id(entry.name) == fid:
            return entry.name
    return None


def _remove_if_empty(folder: Path) -> None:
    leftovers = [p for p in folder.iterdir() if p.name != ".DS_Store"]
    if not leftovers:
        (folder / ".DS_Store").unlink(missing_ok=True)
        folder.rmdir()


def _read_tag(path: Path, key: str) -> str | None:
    """One ID3 text frame ("artist", "genre", "date"), or None."""
    if path.suffix.lower() != ".mp3":
        return None
    try:
        return (EasyID3(path).get(key) or [None])[0]
    except Exception:
        return None


def tag_genre(path: Path, genre: str) -> None:
    """Write the ID3 genre so Serato/rekordbox can sort by it. In place: hard links stay linked."""
    if genre == UNKNOWN or path.suffix.lower() != ".mp3":
        return
    try:
        try:
            tags = EasyID3(path)
        except ID3NoHeaderError:
            tags = EasyID3()
        tags["genre"] = genre
        tags.save(path)
    except Exception as exc:
        log.warning("Could not write genre tag to %s: %s", path.name, exc)


def place(lib: Path, name: str, cat: Category) -> Path:
    """
    Record `cat` for All Songs/<name>, hard-link it into its genre folder, drop
    any stale link in another genre folder, and tag the file. Returns the
    folder it now lives in.
    """
    lib = Path(lib)
    src = lib / ALL_SONGS / name
    target_dir = lib / cat.folder

    with _index_lock:
        for folder in lib.iterdir():
            if not folder.is_dir() or folder.name.startswith(".") or folder.name == ALL_SONGS:
                continue
            old = folder / name
            if folder != target_dir and old.exists() and os.path.samefile(old, src):
                old.unlink()
                _remove_if_empty(folder)

        target_dir.mkdir(exist_ok=True)
        dst = target_dir / name
        if not dst.exists():
            os.link(src, dst)
        elif not os.path.samefile(dst, src):
            log.warning("%s already holds a different file named %s; left it alone", target_dir.name, name)

        index = load_index(lib)
        index[name] = {"genre": cat.genre, "decade": cat.decade, "source": cat.source, "detail": cat.detail}
        _save_index(lib, index)

    tag_genre(src, cat.genre)
    return target_dir


def resolve(lib: Path, name: str, **hints) -> Category:
    """A stored user choice wins; otherwise categorize from scratch."""
    prior = stored(lib, name)
    if prior and prior.source == "user":
        return prior
    return categorize(Path(lib) / ALL_SONGS / name, **hints)


def categorize_and_place(lib: Path, name: str, **hints) -> Category:
    cat = resolve(lib, name, **hints)
    place(lib, name, cat)
    return cat


def set_user_choice(lib: Path, name: str, genre: str, decade: str | None) -> Category:
    cat = Category(genre, decade, "user", "")
    place(lib, name, cat)
    return cat


def unknown_names(lib: Path) -> list[str]:
    """Files whose current folder is Unknown/, alphabetically."""
    return sorted(
        name for name, entry in load_index(lib).items()
        if folder_name(entry.get("genre", UNKNOWN), entry.get("decade"), user_set=entry.get("source") == "user") == UNKNOWN
        and (Path(lib) / ALL_SONGS / name).exists()
    )
