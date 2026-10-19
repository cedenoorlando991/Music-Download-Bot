#!/usr/bin/env python3
"""
categorize_library.py — sort existing libraries into All Songs/ + genre folders.

    categorize-library                       dry run over all three libraries
    categorize-library --apply               do it, reusing the dry run's report
    categorize-library --dry-run "claire!"   one library only (path or DJ/ name)

Dry run looks every song up and writes categorize-report.tsv next to music-dl
without touching any library. The report is saved after every song and songs
already in it are not looked up again, so a stopped run resumes where it left
off (--fresh re-looks everything up, e.g. after changing library.py). Edit the
genre/decade columns in that report if you like — set source to "user" too,
so later guesses never override your edit. --apply uses the report's values
and only looks up files the report doesn't cover.

--apply moves each loose audio file at the library's top level into All Songs/, then
hard-links it into "<Genre> <Decade>/" (or Unknown/). Choices already made in
Telegram are never overwritten.
"""

import argparse
import csv
import logging
import sys
from pathlib import Path

import library
from library import ALL_SONGS, AUDIO_EXTS, Category

DJ_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LIBRARIES = ["Music", "Dalizas Music", "claire!"]
REPORT = DJ_DIR / "categorize-report.tsv"
COLUMNS = ["library", "file", "genre", "decade", "folder", "source", "matched"]


def songs(lib: Path) -> list[tuple[Path, str]]:
    """(current path, filename) for loose top-level files and All Songs/ files."""
    found = []
    for folder in (lib, lib / ALL_SONGS):
        if folder.is_dir():
            found += [(p, p.name) for p in sorted(folder.iterdir()) if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    return found


def write_report(report: dict[tuple[str, str], Category]) -> None:
    tmp = REPORT.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t", lineterminator="\n")
        writer.writerow(COLUMNS)
        for (lib_name, name), c in report.items():
            writer.writerow([lib_name, name, c.genre, c.decade or "", c.folder, c.source, c.detail])
    tmp.replace(REPORT)


def read_report() -> dict[tuple[str, str], Category]:
    if not REPORT.exists():
        return {}
    rows = {}
    with REPORT.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            # Hand edits: a typo'd genre becomes Unknown, a decade like "90s" is dropped.
            genre = row["genre"] if row["genre"] in library.GENRES else library.UNKNOWN
            decade = row["decade"] if row["decade"] in library.DECADES else None
            rows[(row["library"], row["file"])] = Category(genre, decade, row["source"], row["matched"])
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (default)")
    mode.add_argument("--apply", action="store_true", help="move files and create genre folders")
    parser.add_argument("--fresh", action="store_true", help="dry run: ignore the existing report, look everything up again")
    parser.add_argument("libraries", nargs="*", help="library folders (default: Music, Dalizas Music, claire!)")
    args = parser.parse_args()
    logging.basicConfig(format="%(levelname)s %(message)s", level=logging.WARNING)

    libs = [Path(p) if Path(p).is_absolute() else DJ_DIR / p for p in (args.libraries or DEFAULT_LIBRARIES)]
    missing = [lib for lib in libs if not lib.is_dir()]
    if missing and args.libraries:
        print(f"x not a folder: {missing[0]}", file=sys.stderr)
        return 1
    for lib in missing:
        print(f"  (no {lib.name}/ — skipped)")
    libs = [lib for lib in libs if lib.is_dir()]

    report = read_report()
    if args.fresh and not args.apply:
        done = {lib.name for lib in libs}
        report = {key: c for key, c in report.items() if key[0] not in done}
    totals = {}
    for lib in libs:
        items = songs(lib)
        print(f"\n== {lib.name}: {len(items)} songs")
        for i, (path, name) in enumerate(items, 1):
            cat = report.get((lib.name, name))
            prior = library.stored(lib, name)
            if prior and prior.source == "user":
                cat = prior
            if cat is None:
                cat = library.categorize(path)

            if args.apply:
                dest = lib / ALL_SONGS / name
                if path != dest:
                    if dest.exists():
                        print(f"  ! skipped, All Songs/ already has {name}")
                        continue
                    dest.parent.mkdir(exist_ok=True)
                    path.rename(dest)
                library.place(lib, name, cat)
            else:
                report[(lib.name, name)] = cat
                write_report(report)  # every song, so a stopped run can resume

            totals[cat.folder] = totals.get(cat.folder, 0) + 1
            print(f"  [{i}/{len(items)}] {cat.folder:<28} {name}", flush=True)

    if not args.apply:
        print(f"\nReport: {REPORT}")

    print("\nFolders:")
    for folder, count in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count:>4}  {folder}")
    if not args.apply:
        print("\nNothing was moved. Re-run with --apply to do it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
