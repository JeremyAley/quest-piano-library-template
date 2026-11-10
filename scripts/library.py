#!/usr/bin/env python3
"""Turn the files dropped into inbox/ into the catalog the Quest Piano Trainer app reads.

.github/workflows/library.yml runs this on every push that touches inbox/, library/ or library.toml. It runs the
same way on a PC:

    pip install -r .github/requirements.txt
    python scripts/library.py build [--musescore PATH] [--grade-model PATH]
    pip install -r .github/sheet-requirements.txt
    python scripts/library.py sheets

Folders:
    inbox/<file>         anything dropped in: MIDI (.mid .midi .kar), MusicXML (.musicxml .xml .mxl), MuseScore (.mscz .mscx)
    library/<id>/        one folder per song: source.<ext> (the file as it came) and song.json (title, composer, ...)
    catalog/             what the app reads: catalog.json, songs/<id>/level1..4.mid (and each level's sheet music,
                         level<N>.sheet.json and its pages), report.json, report.md

Rules:
    - A file in inbox/ moves to library/<id>/ and becomes a song. Dropping a file with the same name again replaces it;
      the same file under another name is noticed and left out.
    - Only songs whose source, song.json, settings or pianopipe version changed are processed again (catalog/build.lock),
      so unchanged level files keep their sha256 and the app downloads nothing new.
    - One bad file never stops the build: it stays in library/<id>/, the report says why, the rest still updates.
      Only a crash in this script turns the run red. Each song gets MAX_SECONDS to process; an .mxl that unzips past
      MAX_INFLATED, and links (symlinks) instead of files, are refused without being read.
    - Deleting a library/<id>/ folder removes the song from the catalog.
    - pianopipe reads MIDI and MusicXML / MXL itself (since 0.2.0). MuseScore files (.mscz .mscx) go through the
      MuseScore command line (mscore -o song.mid); so does MusicXML with an older pianopipe that can't read it.
    - Sheet music is a second step (`sheets`), after the levels are pushed, so a dropped song plays at once and its
      book follows. It engraves only levels whose file, score, pianopipe or engraver changed (build.lock), each in
      its own process with SHEET_SECONDS, and stops starting new ones after --minutes (the workflow then starts
      another run for the rest). A level that can't be engraved plays without its book; the report says why.

No song titles are printed outside report.md / report.json and the run summary, which stay in this (private) repo.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

MIDI = (".mid", ".midi", ".kar")
MUSICXML = (".musicxml", ".xml", ".mxl")
MUSESCORE = (".mscz", ".mscx")
SUPPORTED = MIDI + MUSICXML + MUSESCORE
MAX_BYTES = 20 * 1024 * 1024
MAX_INFLATED = 32 * 1024 * 1024  # an .mxl's files once unzipped (a zip bomb stops here)
MAX_SECONDS = 180  # reading one song (pianopipe), so one pathological file can't use up the run
SHEET_SECONDS = 180  # engraving one level (a process of its own: Verovio is native code, which no alarm stops)
SHEET_MINUTES = 12  # a run starts no new level after this; the next run (started by the workflow) does the rest
IGNORED = {".gitkeep", "readme.md", "readme.txt", ".ds_store", "thumbs.db", "desktop.ini"}

# What to say about files we can't read, by extension.
NOT_MUSIC = {
    ".pdf": "a PDF is a picture of the music, which can't be read yet. Export MusicXML or MIDI from the app that made it, or download that version instead",
    ".png": "a picture of sheet music can't be read yet. Find the MusicXML or MIDI version",
    ".jpg": "a picture of sheet music can't be read yet. Find the MusicXML or MIDI version",
    ".jpeg": "a picture of sheet music can't be read yet. Find the MusicXML or MIDI version",
    ".mp3": "a recording can't be turned into notes. Find a MIDI or MusicXML version",
    ".wav": "a recording can't be turned into notes. Find a MIDI or MusicXML version",
    ".m4a": "a recording can't be turned into notes. Find a MIDI or MusicXML version",
    ".ogg": "a recording can't be turned into notes. Find a MIDI or MusicXML version",
    ".flac": "a recording can't be turned into notes. Find a MIDI or MusicXML version",
    ".gp": "Guitar Pro files aren't supported. Export MusicXML from it",
    ".gpx": "Guitar Pro files aren't supported. Export MusicXML from it",
    ".sib": "Sibelius files can't be read directly. Export MusicXML from Sibelius",
    ".musx": "Finale files can't be read directly. Export MusicXML from Finale",
    ".dorico": "Dorico files can't be read directly. Export MusicXML from Dorico",
    ".rmi": "RIFF MIDI (.rmi) isn't supported. Save it as a standard .mid",
}

GUESSED = "Hands were guessed: the file doesn't say which notes are the left and which the right hand"


# ------------------------------------------------------------------ settings


@dataclass
class Settings:
    name: str = "My library"
    fingering: bool = True
    musescore: bool = True
    melody: bool = False
    sheets: bool = True

    @classmethod
    def load(cls, root: Path) -> "Settings":
        path = root / "library.toml"
        s = cls()
        if path.exists():
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
                print(f"::warning::library.toml couldn't be read ({exc.__class__.__name__}); using the default settings")
                return s
            s.name = str(data.get("name", s.name)) or s.name
            s.fingering = bool(data.get("fingering", s.fingering))
            s.musescore = bool(data.get("musescore", s.musescore))
            s.melody = bool(data.get("melody", s.melody))
            s.sheets = bool(data.get("sheets", s.sheets))
        return s

    def key(self) -> str:
        return f"fingering={self.fingering}" + (" melody=True" if self.melody else "")


# ------------------------------------------------------------------ helpers


def slugify(text: str) -> str:
    """'Für Elise (1)' -> 'fur-elise-1': the catalog's id rule (lowercase letters, digits and dashes)."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:60].strip("-") or "song"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def title_from_name(name: str) -> str:
    """'moonlight_sonata-1st_mvt (2).mid' -> 'Moonlight sonata 1st mvt'."""
    stem = Path(name).stem
    stem = re.sub(r"\s*\(\d+\)$", "", stem)  # browser download copies: "song (1)"
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"\s*-\s*", " - ", stem) if " - " in stem else stem.replace("-", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    return (stem[:1].upper() + stem[1:]) if stem else "Untitled"


def _score_root(data: bytes, ext: str):
    """A MusicXML or MXL file's root element, or None."""
    try:
        if ext == ".mxl" or data[:2] == b"PK":
            with zipfile.ZipFile(_bytes_io(data)) as z:
                inner = _mxl_root(z)
                if inner is None:
                    return None
                data = read_capped(z, inner)
        if b"<!ENTITY" in data[:65536]:
            return None  # entity declarations: not something a score needs, and they can expand without end
        return ElementTree.fromstring(data)
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError, OSError, RuntimeError, NotImplementedError):
        return None


def musicxml_info(data: bytes, ext: str) -> tuple[str, str]:
    """(title, composer) from a MusicXML or MXL file's header, or ("", "")."""
    root = _score_root(data, ext)
    if root is None:
        return "", ""
    title = (root.findtext("work/work-title") or root.findtext("movement-title") or "").strip()
    composer = ""
    for c in root.findall("identification/creator"):
        if c.get("type") == "composer" and (c.text or "").strip():
            composer = c.text.strip()
            break
    return title, composer


def musicxml_book(data: bytes, ext: str) -> str:
    """The songbook a MusicXML file was read from: its <miscellaneous-field name="book"> (tools/sheet_to_library.py
    writes it), or ""."""
    root = _score_root(data, ext)
    if root is None:
        return ""
    for f in root.findall("identification/miscellaneous/miscellaneous-field"):
        if f.get("name") == "book" and (f.text or "").strip():
            return " ".join(f.text.split())
    return ""


def book_tags(meta: dict) -> list[str]:
    """song.json's "book" as the catalog's tag ("book:Contemporary Disney"; docs/formats.md), or none."""
    book = meta.get("book")
    return [f"book:{' '.join(book.split())}"] if isinstance(book, str) and book.strip() else []


def _bytes_io(data: bytes):
    import io

    return io.BytesIO(data)


def read_capped(z: zipfile.ZipFile, name: str, limit: int = MAX_INFLATED) -> bytes:
    """One file from a zip, read no further than `limit` bytes unzipped (ValueError past it, whatever its header says)."""
    out = bytearray()
    with z.open(name) as f:
        while chunk := f.read(65536):
            out += chunk
            if len(out) > limit:
                raise ValueError(f"it unzips to over {limit // 2**20} MB")
    return bytes(out)


def check_mxl(path: Path) -> str:
    """Why an .mxl can't be read safely ("" if it can): too big unzipped, or not a zip at all."""
    try:
        with zipfile.ZipFile(path) as z:
            total = sum(i.file_size for i in z.infolist())
            if total > MAX_INFLATED:
                return f"it unzips to over {MAX_INFLATED // 2**20} MB, too big to be a score"
            inner = _mxl_root(z)
            if inner is not None:
                read_capped(z, inner)  # the header could lie: unzip the score itself, no further than the limit
    except zipfile.BadZipFile:
        return "it isn't a readable .mxl (zip) file"
    except (ValueError, KeyError, OSError, RuntimeError, NotImplementedError) as exc:
        return f"it couldn't be unzipped safely ({str(exc)[:120] or exc.__class__.__name__})"
    return ""


def _mxl_root(z: zipfile.ZipFile) -> str | None:
    """The score inside an .mxl: META-INF/container.xml's rootfile, else the first .xml/.musicxml that isn't META-INF."""
    try:
        container = ElementTree.fromstring(read_capped(z, "META-INF/container.xml", 1024 * 1024))
        for el in container.iter():
            if el.tag.endswith("rootfile") and el.get("full-path"):
                return el.get("full-path")
    except (KeyError, ValueError, ElementTree.ParseError):
        pass
    for n in z.namelist():
        if not n.startswith("META-INF") and n.lower().endswith((".xml", ".musicxml")):
            return n
    return None


@contextlib.contextmanager
def time_limit(seconds: int):
    """Stops what runs inside with TimeoutError after `seconds` (SIGALRM: Linux runners; elsewhere no limit)."""
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def stop(signum, frame):
        raise TimeoutError(f"it took over {seconds} s to process")

    old = signal.signal(signal.SIGALRM, stop)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def inbox_files(inbox: Path) -> list[Path]:
    """Every file under inbox/, sorted, without going into linked folders (the links themselves are listed)."""
    found = []
    for folder, dirs, files in os.walk(inbox, followlinks=False):
        here = Path(folder)
        found += [here / d for d in dirs if (here / d).is_symlink()]
        found += [here / f for f in files]
    return sorted(found)


def format_of(ext: str) -> str:
    return "midi" if ext in MIDI else "musicxml" if ext in MUSICXML else "musescore" if ext in MUSESCORE else "other"


def pianopipe_version() -> str:
    try:
        from importlib.metadata import version

        return version("pianopipe")
    except Exception:  # noqa: BLE001 - reported, not fatal
        return "unknown"


def pianopipe_reads_musicxml() -> bool:
    return importlib.util.find_spec("pianopipe.musicxml") is not None


# ------------------------------------------------------------------ the library on disk


@dataclass
class Item:
    """One song folder's outcome, for the report."""

    id: str
    file: str  # the name it was dropped as
    title: str = ""
    status: str = "ok"  # ok | skipped | failed
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    hand_source: str = ""
    levels: int = 0
    change: str = ""  # added | updated | removed | "" (unchanged this run)
    added: str = ""
    sheets: int = 0  # levels with sheet music
    sheets_pending: int = 0  # levels still to be engraved (a later step or run)
    sheet_notes: list[str] = field(default_factory=list)  # levels without sheet music, and why

    def to_json(self) -> dict:
        return {
            "id": self.id, "file": self.file, "title": self.title, "status": self.status, "reasons": self.reasons,
            "warnings": self.warnings, "hand_source": self.hand_source, "levels": self.levels, "change": self.change,
            "added": self.added, "sheets": self.sheets, "sheets_pending": self.sheets_pending, "sheet_notes": self.sheet_notes,
        }


class Library:
    def __init__(self, root: Path, settings: Settings | None = None, musescore: str | None = None, grade_model=None,
                 now: dt.datetime | None = None, retry_failed: bool = False):
        self.root = Path(root)
        self.inbox = self.root / "inbox"
        self.songs_dir = self.root / "library"
        self.catalog_dir = self.root / "catalog"
        self.settings = settings or Settings.load(self.root)
        self.musescore = musescore
        self.grade_model = grade_model
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self.retry_failed = retry_failed
        self.items: dict[str, Item] = {}
        self.entries: dict = {}  # id -> the catalog's SongEntry, once built or loaded
        self.inbox_notes: list[str] = []  # inbox files that didn't become songs (duplicates, too big), for the report
        self.sheet_line = ""  # what the sheet music step did this run, for the report
        self.lock_path = self.catalog_dir / "build.lock"
        self.lock = self._read_lock()

    # ---- state

    def _read_lock(self) -> dict:
        try:
            return json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"songs": {}}

    def song_json(self, sid: str) -> dict:
        try:
            return json.loads((self.songs_dir / sid / "song.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def source_of(self, sid: str) -> Path | None:
        for p in sorted((self.songs_dir / sid).glob("source.*")):
            return p
        return None

    # ---- step 1: inbox -> library/<id>/

    def ingest(self) -> list[str]:
        """Moves every inbox file into its own library folder; returns the ids it touched."""
        if not self.inbox.exists():
            return []
        by_name, by_sha = {}, {}
        for d in self.songs_dir.glob("*/") if self.songs_dir.exists() else []:
            meta = self.song_json(d.name)
            if meta.get("original_name"):
                by_name[meta["original_name"].lower()] = d.name
            if meta.get("source_sha256"):
                by_sha[meta["source_sha256"]] = d.name
        touched = []
        for path in inbox_files(self.inbox):
            name = path.name
            if name.lower() in IGNORED or name.startswith("."):
                continue
            try:
                self._ingest_one(path, by_name, by_sha, touched)
            except Exception as exc:  # noqa: BLE001 - one bad file never stops the build
                self.inbox_notes.append(f"{name}: left out, it couldn't be taken in ({_plain_error(exc)})")
                try:
                    path.unlink()
                except OSError:
                    pass
        return touched

    def _ingest_one(self, path: Path, by_name: dict, by_sha: dict, touched: list[str]) -> None:
        name = path.name
        if path.is_symlink():
            # A link could point anywhere on the runner (the checkout's .git, say): never read through it.
            self.inbox_notes.append(f"{name}: left out, it's a link (symlink), not a file")
            path.unlink()
            return
        size = path.stat().st_size
        if size > MAX_BYTES:
            self.inbox_notes.append(f"{name}: left out, it's {size / 1e6:.0f} MB (the limit is {MAX_BYTES // 2**20} MB)")
            path.unlink()
            return
        data = path.read_bytes()
        digest = sha256(data)
        ext = path.suffix.lower()
        if digest in by_sha and by_name.get(name.lower()) != by_sha[digest]:
            other = by_sha[digest]
            self.inbox_notes.append(f"{name}: left out, it's the same file as {self.song_json(other).get('title') or other}")
            path.unlink()
            return
        # Read before anything moves, so a file that can't be read is left out whole.
        title, composer = musicxml_info(data, ext) if ext in MUSICXML else ("", "")
        book = musicxml_book(data, ext) if ext in MUSICXML else ""
        sid = by_name.get(name.lower())
        replacing = sid is not None
        if sid is None:
            sid = base = slugify(Path(name).stem)
            n = 2
            while (self.songs_dir / sid).exists() or sid in touched:
                sid = f"{base}-{n}"
                n += 1
        folder = self.songs_dir / sid
        folder.mkdir(parents=True, exist_ok=True)
        meta = self.song_json(sid) if replacing else {}
        for old in folder.glob("source.*"):
            old.unlink()
        shutil.move(str(path), folder / f"source{ext}")
        # A replaced file keeps its song's title (it may have been fixed by hand in song.json).
        meta["title"] = meta.get("title") or title or title_from_name(name)
        if composer and not meta.get("composer"):
            meta["composer"] = composer
        meta.setdefault("composer", "")
        if book:  # the songbook it was scanned from (tools/sheet_to_library.py); songs.json there changes it
            meta["book"] = book
        meta["original_name"] = name
        meta["format"] = format_of(ext)
        meta["source_sha256"] = digest
        meta.setdefault("added", self.now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        meta["updated"] = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")
        (folder / "song.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        by_name[name.lower()] = sid
        by_sha[digest] = sid
        touched.append(sid)

    # ---- step 2: library/<id>/ -> catalog/

    def needs_musescore(self) -> bool:
        """Whether this run will convert anything with MuseScore (the workflow only downloads it then)."""
        reads_xml = pianopipe_reads_musicxml()
        exts = [p.suffix.lower() for p in inbox_files(self.inbox) if not p.is_symlink()] if self.inbox.exists() else []
        for d in sorted(self.songs_dir.glob("*/")) if self.songs_dir.exists() else []:
            src = self.source_of(d.name)
            if src is None or src.is_symlink():
                continue
            prev = self.lock["songs"].get(d.name, {})
            try:
                key = self._key(d.name, src)
            except OSError:
                continue
            if key != prev.get("key") or (self.retry_failed and prev.get("status") == "failed"):
                exts.append(src.suffix.lower())
        return any(self._converts(e, reads_xml) for e in exts)

    @staticmethod
    def _converts(ext: str, reads_xml: bool | None = None) -> bool:
        """Whether files with this extension go through MuseScore."""
        if reads_xml is None:
            reads_xml = pianopipe_reads_musicxml()
        return ext in MUSESCORE or (ext in MUSICXML and not reads_xml)

    def _key(self, sid: str, src: Path) -> str:
        # song.json by content, so line endings or spacing (a Windows checkout, an edit on github.com) don't matter.
        # Its "book" only labels the catalog entry (_build_one sets it on a reused song), so it isn't processed again.
        meta = json.dumps({k: v for k, v in self.song_json(sid).items() if k != "book"}, sort_keys=True).encode()
        # A different converter (MuseScore version) is a reason to try the files it converts again.
        tool = os.environ.get("CONVERTER", "") if self._converts(src.suffix.lower()) else ""
        return sha256(src.read_bytes() + b"\0" + meta + b"\0" + f"{pianopipe_version()} {self.settings.key()} {tool}".encode())

    def build(self, touched: list[str] | None = None) -> None:
        from pianopipe.catalog import Catalog

        touched = set(touched or [])
        self.catalog_dir.mkdir(parents=True, exist_ok=True)
        songs_out = self.catalog_dir / "songs"
        old_entries = {}
        cat_path = self.catalog_dir / "catalog.json"
        if cat_path.exists():
            try:
                old_entries = {s.id: s for s in Catalog.load(cat_path).songs}
            except (ValueError, KeyError, TypeError):
                old_entries = {}
        lock_songs = self.lock.setdefault("songs", {})
        entries = {}
        ids = sorted(d.name for d in self.songs_dir.glob("*/")) if self.songs_dir.exists() else []
        for sid in ids:
            meta = self.song_json(sid)
            src = self.source_of(sid)
            item = Item(id=sid, file=meta.get("original_name", src.name if src else sid), title=meta.get("title", sid),
                        added=meta.get("added", ""))
            try:
                self._build_one(sid, src, meta, item, lock_songs, old_entries, entries, touched, songs_out)
            except Exception as exc:  # noqa: BLE001 - one bad file never stops the build
                item.status, item.reasons, item.change = "failed", [_plain_error(exc)], item.change or "updated"
                entries.pop(sid, None)
                self.items[sid] = item
                lock_songs[sid] = {"key": "", **_outcome(item)}
        self._finish(ids, lock_songs, old_entries, entries, songs_out, cat_path)

    def _build_one(self, sid, src, meta, item, lock_songs, old_entries, entries, touched, songs_out) -> None:
        """One song folder: reused from the last run when nothing about it changed, else processed again."""
        prev = lock_songs.get(sid, {})
        if src is None or src.is_symlink():
            item.status = "failed"
            item.reasons = ["the folder has no source file (source.mid, source.musicxml, ...)" if src is None
                            else "its source file is a link (symlink), not a file"]
            self.items[sid] = item
            lock_songs[sid] = {"key": "", **_outcome(item)}
            return
        key = self._key(sid, src)
        retry = self.retry_failed and prev.get("status") == "failed"
        reuse = (prev.get("key") == key and sid not in touched and not retry and
                 (prev.get("status") != "ok" or (sid in old_entries and _levels_present(old_entries[sid], self.catalog_dir))))
        if reuse:
            item.status, item.reasons, item.warnings = prev.get("status", "ok"), prev.get("reasons", []), prev.get("warnings", [])
            item.hand_source, item.levels = prev.get("hand_source", ""), prev.get("levels", 0)
            if item.status == "ok":
                entry = old_entries[sid]
                entry.tags = [t for t in entry.tags if not t.startswith("book:")] + book_tags(meta)
                entries[sid] = entry
        else:
            item.change = "updated" if sid in old_entries or prev else "added"
            shutil.rmtree(songs_out / sid, ignore_errors=True)
            entry = self._process(sid, src, meta, item)
            if entry is not None:
                entries[sid] = entry
        self.items[sid] = item
        # A reused song keeps its sheet music's records; a processed one has new levels to engrave.
        lock_songs[sid] = {"key": key, **_outcome(item), **({"sheets": prev["sheets"]} if reuse and "sheets" in prev else {})}

    def _finish(self, ids, lock_songs, old_entries, entries, songs_out, cat_path) -> None:
        """Removed songs out, then catalog.json and build.lock written."""
        from pianopipe.catalog import Catalog

        for sid in list(lock_songs) + list(old_entries):
            if sid not in ids and (sid in lock_songs or sid in old_entries):
                if sid not in self.items:
                    self.items[sid] = Item(id=sid, file=lock_songs.get(sid, {}).get("file", sid),
                                           title=lock_songs.get(sid, {}).get("title", sid), change="removed", status="removed")
                lock_songs.pop(sid, None)
                shutil.rmtree(songs_out / sid, ignore_errors=True)
        # Newest first: the app's "My songs" shelf shows the latest additions first.
        order = sorted(entries, key=lambda s: (self.items[s].added, s), reverse=True)
        catalog = Catalog(name=self.settings.name, songs=[entries[s] for s in order])
        catalog.save(cat_path)
        self.entries = dict(entries)
        self.lock["pianopipe"] = pianopipe_version()
        self.lock["settings"] = self.settings.key()
        self._write_lock()

    def _write_lock(self) -> None:
        self.lock_path.write_text(json.dumps(self.lock, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

    def _process(self, sid: str, src: Path, meta: dict, item: Item):
        from pianopipe.process import process_file

        ext = src.suffix.lower()
        if ext not in SUPPORTED:
            item.status = "skipped"
            item.reasons = [NOT_MUSIC.get(ext, f"“{ext or 'no extension'}” isn't a music file this library reads "
                                               "(MIDI, MusicXML, MXL or MuseScore)")]
            return None
        if ext == ".mxl":
            why = check_mxl(src)
            if why:
                item.status, item.reasons = "failed", [why]
                return None
        work = Path(tempfile.mkdtemp(prefix="library-"))
        try:
            midi = src
            if self._converts(ext):
                midi, error = self._convert(src, work)
                if midi is None:
                    item.status, item.reasons = "failed", [error]
                    return None
            fields = {k: meta[k] for k in ("title", "full_title", "composer", "era", "character") if meta.get(k)}
            fields["id"] = sid
            fields["tags"] = [t for t in meta.get("tags", []) if isinstance(t, str) and not t.startswith("book:")] + book_tags(meta)
            # A song's voice or melody part merged into the right hand: song.json's "melody" (true, false, or the
            # parts' names), else library.toml's. A MIDI file has no parts to merge; pianopipe ignores it there.
            melody = meta.get("melody", self.settings.melody)
            if melody and isinstance(melody, (bool, str, list)):
                fields["melody"] = melody
            kwargs = {"fingering": self.settings.fingering}
            if self.grade_model is not None:
                kwargs["grade_model"] = self.grade_model
            with time_limit(MAX_SECONDS):
                entry = process_file(midi, self.catalog_dir / "songs", fields, "songs/", **kwargs)
        except Exception as exc:  # noqa: BLE001 - one bad file never stops the build
            item.status, item.reasons = "failed", [_plain_error(exc)]
            return None
        finally:
            shutil.rmtree(work, ignore_errors=True)
        item.hand_source = entry.hand_source
        item.levels = len(entry.levels)
        item.title = entry.title
        if entry.hand_source == "auto":
            item.warnings.append(GUESSED)
        # The melody merge's own notes (which parts, the bars it skipped), then the gate's.
        item.warnings += [w[8].upper() + w[9:] for w in entry.warnings if w.startswith("melody: ")]
        hard = [w for w in entry.warnings if not w.startswith("melody: ")]
        if hard:
            shown = hard[:5]
            more = len(hard) - len(shown)
            item.warnings.append("Hard to play as written at " + "; ".join(shown) + (f" (and {more} more)" if more > 0 else ""))
        return entry

    def _convert(self, src: Path, work: Path) -> tuple[Path | None, str]:
        """MusicXML / MuseScore -> MIDI with the MuseScore CLI. Returns (midi path, "") or (None, why not)."""
        kind = "MuseScore" if src.suffix.lower() in MUSESCORE else "MusicXML"
        if not self.settings.musescore:
            return None, f"{kind} files need MuseScore, which is turned off in library.toml (musescore = false)"
        if not self.musescore:
            return None, f"{kind} files need MuseScore to convert them, and it wasn't available in this run"
        out = work / "song.mid"
        env = dict(os.environ, APPIMAGE_EXTRACT_AND_RUN="1")
        try:
            run = subprocess.run([self.musescore, "-o", str(out), str(src)], capture_output=True, text=True, timeout=180, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"MuseScore couldn't convert it ({exc.__class__.__name__})"
        if not out.exists() or out.stat().st_size == 0:
            tail = (run.stderr or run.stdout or "").strip().splitlines()[-1:] or [f"exit code {run.returncode}"]
            return None, f"MuseScore couldn't read this {kind} file ({tail[0][:200]})"
        return out, ""

    # ---- step 3: sheet music (catalog/songs/<id>/level<N>.sheet.json and its pages, after the levels are pushed)

    def load_catalog(self) -> None:
        """catalog.json's songs, and each song's report item from build.lock: for the sheet step, which runs on its own."""
        from pianopipe.catalog import Catalog

        cat_path = self.catalog_dir / "catalog.json"
        try:
            self.entries = {s.id: s for s in Catalog.load(cat_path).songs} if cat_path.exists() else {}
        except (ValueError, KeyError, TypeError):
            self.entries = {}
        for sid, rec in self.lock.get("songs", {}).items():
            self.items[sid] = Item(id=sid, file=rec.get("file", sid), title=rec.get("title", sid), status=rec.get("status", "ok"),
                                   reasons=rec.get("reasons", []), warnings=rec.get("warnings", []),
                                   hand_source=rec.get("hand_source", ""), levels=rec.get("levels", 0),
                                   added=self.song_json(sid).get("added", ""))
        # The report's "This run" (the build step's, earlier in the same run) stays.
        try:
            last = json.loads((self.catalog_dir / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            last = {}
        for i in last.get("items", []):
            if i.get("id") in self.items:
                self.items[i["id"]].change = i.get("change", "")
        for sid in last.get("removed", []):
            self.items.setdefault(sid, Item(id=sid, file=sid, title=sid, change="removed", status="removed"))
        self.inbox_notes = list(last.get("inbox", []))

    def sheet_pins(self) -> str:
        """The engraver's pinned wheels (.github/sheet-requirements.txt): a new engraver engraves every level again."""
        try:
            text = (self.root / ".github" / "sheet-requirements.txt").read_text(encoding="utf-8")
        except OSError:
            return ""
        return sha256("\n".join(l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")).encode())

    def score_for(self, sid: str, entry, level: int) -> Path | None:
        """The score a song's full level shows its own notation from (as pianopipe's process_file chooses it): a
        MusicXML source whose staves gave the hands, unless a melody part was merged in (the score's keyboard part
        lacks it): the build's "Merged ... into the right hand" warning says so."""
        if level != max(lv.level for lv in entry.levels) or entry.hand_source != "staves":
            return None
        src = self.source_of(sid)
        if src is None or src.is_symlink() or src.suffix.lower() not in MUSICXML:
            return None
        warnings = self.lock.get("songs", {}).get(sid, {}).get("warnings", [])
        return None if any(w.startswith("Merged ") and "into the right hand" in w for w in warnings) else src

    def sheet_jobs(self) -> list[dict]:
        """The levels whose sheet music is missing or out of date (level file, score, pianopipe or engraver changed)."""
        if not self.settings.sheets:
            return []
        pins, version = self.sheet_pins(), pianopipe_version()
        jobs = []
        for sid, entry in self.entries.items():
            done = self.lock.get("songs", {}).get(sid, {}).get("sheets", {})
            meta = self.song_json(sid)
            for lv in entry.levels:
                if "://" in lv.url or not lv.url.endswith(".mid"):
                    continue
                score = self.score_for(sid, entry, lv.level)
                key = sha256(f"{lv.sha256} {sha256(score.read_bytes()) if score else ''} {version} {pins}".encode())
                rec = done.get(str(lv.level), {})
                if rec.get("key") == key:
                    if rec.get("status") == "ok" and lv.sheet is not None and self._sheet_present(lv):
                        continue
                    if rec.get("status") == "failed" and not self.retry_failed:
                        continue
                midi = self.catalog_dir / lv.url
                jobs.append({"id": sid, "level": lv.level, "key": key, "midi": str(midi), "out": str(midi.parent),
                             "title": entry.title, "composer": entry.composer or "",
                             "tempo": meta.get("tempo", "") if isinstance(meta.get("tempo"), str) else "",
                             "source": str(score) if score else None})
        return jobs

    def _sheet_present(self, lv) -> bool:
        path = self.catalog_dir / lv.sheet.url
        try:
            pages = json.loads(path.read_text(encoding="utf-8"))["pages"]
            return all((path.parent / p["file"]).exists() for p in pages)
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def sheets_needed(self) -> bool:
        """Whether the sheet step has anything to do: levels to engrave, or (sheets = false) sheet music to remove."""
        if not self.settings.sheets:
            return any(lv.sheet is not None for e in self.entries.values() for lv in e.levels)
        return bool(self.sheet_jobs())

    def engrave_sheets(self, minutes: float = SHEET_MINUTES, workers: int | None = None) -> dict:
        """Engraves what sheet_jobs() lists, a few levels at a time, each in its own process; starts none after
        `minutes`. Then writes the catalog's sheet entries, build.lock and the reports. Returns counts for the commit
        message: engraved and failed levels, songs touched, and levels left for the next run."""
        from pianopipe.catalog import Catalog, SheetRef

        if not self.settings.sheets:
            removed = 0
            for sid, entry in self.entries.items():
                for lv in entry.levels:
                    if lv.sheet is not None:
                        _clear_sheet(self.catalog_dir / lv.url, lv.level)
                        lv.sheet, removed = None, removed + 1
                self.lock.get("songs", {}).get(sid, {}).pop("sheets", None)
            counts = {"engraved": 0, "failed": 0, "songs": 0, "left": 0, "removed": removed}
        else:
            jobs = self.sheet_jobs()
            deadline = time.monotonic() + minutes * 60
            work = Path(tempfile.mkdtemp(prefix="sheets-"))

            def one(numbered):
                n, job = numbered
                return None if time.monotonic() > deadline else _engrave_in_child(job, work / f"job{n}")

            import concurrent.futures

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers or os.cpu_count() or 2) as pool:
                    results = [(job, r) for job, r in zip(jobs, pool.map(one, enumerate(jobs))) if r is not None]
            finally:
                shutil.rmtree(work, ignore_errors=True)
            for job, res in results:
                lv = next(l for l in self.entries[job["id"]].levels if l.level == job["level"])
                recs = self.lock.setdefault("songs", {}).setdefault(job["id"], {}).setdefault("sheets", {})
                if res["status"] == "ok":
                    lv.sheet = SheetRef(f"{lv.url.rsplit('/', 1)[0]}/level{lv.level}.sheet.json", res["sha256"], res["pages"])
                    recs[str(lv.level)] = {"key": job["key"], "status": "ok", "pages": res["pages"],
                                           **({"note": res["note"]} if res.get("note") else {})}
                else:
                    _clear_sheet(Path(job["midi"]), lv.level)
                    lv.sheet = None
                    recs[str(lv.level)] = {"key": job["key"], "status": "failed", "reason": res["reason"]}
            failed = sum(r["status"] != "ok" for _, r in results)
            counts = {"engraved": len(results) - failed, "failed": failed, "songs": len({j["id"] for j, _ in results}),
                      "left": len(jobs) - len(results), "removed": 0}
        Catalog(name=self.settings.name, songs=list(self.entries.values())).save(self.catalog_dir / "catalog.json")
        self._write_lock()
        self.sheet_line = sheet_line(counts)
        return counts

    # ---- step 4: reports

    def _sheet_status(self, item: Item) -> None:
        entry = self.entries.get(item.id)
        if entry is None or not self.settings.sheets:
            return
        recs = self.lock.get("songs", {}).get(item.id, {}).get("sheets", {})
        item.sheets = sum(lv.sheet is not None for lv in entry.levels)
        failed = [(lv.level, recs[str(lv.level)].get("reason", "")) for lv in entry.levels
                  if lv.sheet is None and recs.get(str(lv.level), {}).get("status") == "failed"]
        item.sheet_notes = [f"level {n}: {why}" for n, why in failed]
        item.sheets_pending = len(entry.levels) - item.sheets - len(failed)

    def report(self, run_url: str = "") -> dict:
        for i in self.items.values():
            self._sheet_status(i)
        items = sorted(self.items.values(), key=lambda i: (i.status == "ok", i.title.lower()))
        counts = {s: sum(i.status == s for i in items) for s in ("ok", "skipped", "failed")}
        attention = [i for i in items if i.status in ("skipped", "failed") or i.warnings]
        ok = [i for i in items if i.status == "ok"]
        return {
            "version": 1,
            "generated": self.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pianopipe": pianopipe_version(),
            "run_url": run_url,
            "songs": counts["ok"],
            "needs_attention": len(attention),
            "counts": counts,
            "inbox": self.inbox_notes,
            "items": [i.to_json() for i in items if i.status != "removed"],
            "removed": [i.id for i in items if i.status == "removed"],
            # Sheet music: levels with it, of all the ready songs' levels; still to engrave; couldn't be engraved.
            "sheets": {"on": self.settings.sheets, "levels": sum(i.sheets for i in ok), "of": sum(i.levels for i in ok),
                       "pending": sum(i.sheets_pending for i in ok), "failed": sum(len(i.sheet_notes) for i in ok)},
            "sheet_run": self.sheet_line,
        }

    def changed(self) -> bool:
        return bool(self.inbox_notes) or any(i.change for i in self.items.values())

    def write_reports(self, run_url: str = "", force: bool = False) -> dict:
        rep = self.report(run_url)
        if not force and not self.changed() and (self.catalog_dir / "report.json").exists():
            # Nothing happened: keep the last report (and its "This run"), so there's nothing to commit.
            try:
                return json.loads((self.catalog_dir / "report.json").read_text(encoding="utf-8"))
            except ValueError:
                pass
        (self.catalog_dir / "report.json").write_text(json.dumps(rep, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        (self.catalog_dir / "report.md").write_text(markdown(rep), encoding="utf-8", newline="\n")
        return rep


def _levels_present(entry, catalog_dir: Path) -> bool:
    return all((catalog_dir / lv.url).exists() for lv in entry.levels)


def _outcome(item: Item) -> dict:
    return {"status": item.status, "reasons": item.reasons, "warnings": item.warnings, "hand_source": item.hand_source,
            "levels": item.levels, "file": item.file, "title": item.title}


def _plain_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if "no playable notes" in text:
        return "there are no notes in it to play"
    if exc.__class__.__name__ in ("OSError", "EOFError", "IndexError", "KeyError") or "MThd" in text or "data byte" in text:
        return f"couldn't read it as MIDI ({text[:160]}). Try saving it again from the app that made it"
    return f"couldn't process it: {text[:200]}"


def _clear_sheet(level_midi: Path, level: int) -> None:
    """A level's sheet music files, gone (before it's engraved again, or when it couldn't be)."""
    folder = Path(level_midi).parent
    for p in [folder / f"level{level}.sheet.json", *folder.glob(f"level{level}-p*.png")]:
        p.unlink(missing_ok=True)


def engraver_missing() -> str:
    """Why the engraver can't be loaded ("" when it can)."""
    try:
        import resvg_py  # noqa: F401
        import verovio  # noqa: F401
        from PIL import Image  # noqa: F401
        from pianopipe import engrave  # noqa: F401
    except ImportError as exc:
        return str(exc)[:200]
    return ""


def engrave_level(job: dict) -> dict:
    """One level's sheet music (pianopipe's engrave): {"status": "ok", sha256, pages, note} or {"status": "failed", reason}."""
    from pianopipe import engrave

    midi, level = Path(job["midi"]), int(job["level"])
    _clear_sheet(midi, level)
    try:
        sheet = engrave.engrave(midi, midi.parent, f"level{level}", level, job["title"], source=job.get("source"),
                                composer=job.get("composer", ""), tempo=job.get("tempo", ""))
    except Exception as exc:  # noqa: BLE001 - any engraving failure is the level's sheet music's, not the song's
        _clear_sheet(midi, level)
        return {"status": "failed", "reason": (str(exc).strip() or exc.__class__.__name__)[:200]}
    return {"status": "ok", "sha256": sheet.sha256, "pages": sheet.pages, "note": sheet.note}


def _engrave_in_child(job: dict, tmp: Path) -> dict:
    """engrave_level in a process of its own, so a level that crashes Verovio or takes too long costs only itself."""
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "job.json").write_text(json.dumps(job), encoding="utf-8")
    try:
        run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "engrave-level", str(tmp / "job.json"),
                              str(tmp / "result.json")], capture_output=True, text=True, timeout=SHEET_SECONDS)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": f"it took over {SHEET_SECONDS} s to engrave"}
    try:
        return json.loads((tmp / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tail = (run.stderr or run.stdout or "").strip().splitlines()[-1:] or [f"exit code {run.returncode}"]
        return {"status": "failed", "reason": f"the engraver stopped ({tail[0][:160]})"}


def sheet_line(counts: dict) -> str:
    """What the sheet step did, in a few words: the commit message and report.md's "This run"."""
    if counts.get("removed"):
        return f"sheet music removed from {counts['removed']} levels (sheets = false)"
    parts = []
    if counts["engraved"]:
        parts.append(f"sheet music for {counts['engraved']} level{'s' if counts['engraved'] != 1 else ''} "
                     f"of {counts['songs']} song{'s' if counts['songs'] != 1 else ''}")
    if counts["failed"]:
        parts.append(f"{counts['failed']} level{'s' if counts['failed'] != 1 else ''} couldn't be engraved")
    if counts["left"]:
        parts.append(f"{counts['left']} still to engrave (the next run)")
    return ", ".join(parts)


# ------------------------------------------------------------------ report.md


def markdown(rep: dict) -> str:
    lines = ["# Library report", ""]
    when = rep["generated"].replace("T", " ").replace("Z", " UTC")
    run = f" · [run log]({rep['run_url']})" if rep.get("run_url") else ""
    lines.append(f"Last run {when} · pianopipe {rep['pianopipe']}{run}")
    lines.append("")
    c = rep["counts"]
    attention = rep["needs_attention"]
    summary = f"**{rep['songs']} song{'s' if rep['songs'] != 1 else ''} ready**"
    if attention:
        summary += f", {attention} need{'s' if attention == 1 else ''} attention"
    if c["failed"] or c["skipped"]:
        summary += f" ({c['failed']} couldn't be read, {c['skipped']} skipped)"
    lines += [summary + ".", ""]
    sheets = rep.get("sheets") or {}
    if sheets.get("on") and sheets.get("of"):
        line = f"Sheet music for {sheets['levels']} of {sheets['of']} levels"
        if sheets["pending"]:
            line += f"; {sheets['pending']} still being engraved (a few minutes after the levels)"
        if sheets["failed"]:
            line += f"; {sheets['failed']} couldn't be engraved (they play without the book)"
        lines += [line + ".", ""]
    items = rep["items"]
    changed = [i for i in items if i["change"]]
    if changed or rep["removed"] or rep["inbox"] or rep.get("sheet_run"):
        lines += ["## This run", ""]
        for i in changed:
            what = {"ok": "ready", "failed": "couldn't be read", "skipped": "skipped"}[i["status"]]
            lines.append(f"- {i['change'].capitalize()}: **{_md(i['title'])}** (`{_md(i['file'])}`): {what}")
        for sid in rep["removed"]:
            lines.append(f"- Removed: `{sid}`")
        for note in rep["inbox"]:
            lines.append(f"- {_md(note)}")
        if rep.get("sheet_run"):
            lines.append(f"- {_md(rep['sheet_run'][:1].upper() + rep['sheet_run'][1:])}")
        lines.append("")
    bad = [i for i in items if i["status"] != "ok"]
    warned = [i for i in items if i["status"] == "ok" and i["warnings"]]
    if bad or warned:
        lines += ["## Needs attention", ""]
        for i in bad:
            lines.append(f"- **{_md(i['title'])}** (`{_md(i['file'])}`, folder `library/{i['id']}/`): "
                         f"{'skipped' if i['status'] == 'skipped' else 'couldn’t be read'}: {_md('; '.join(i['reasons']))}. "
                         f"Drop a better file with the same name to replace it, or delete the folder.")
        for i in warned:
            for w in i["warnings"]:
                lines.append(f"- **{_md(i['title'])}**: {_md(w)}.")
        lines.append("")
    ok = [i for i in items if i["status"] == "ok"]
    unsheeted = [i for i in ok if i.get("sheet_notes")]
    if sheets.get("on") and unsheeted:
        lines += ["## Sheet music", "", "These levels play without the book, because they couldn't be engraved:", ""]
        for i in sorted(unsheeted, key=lambda i: i["title"].lower()):
            lines.append(f"- **{_md(i['title'])}**: {_md('; '.join(i['sheet_notes']))}.")
        lines += ["", "To engrave them again: Actions → Library → Run workflow, with “Try again what couldn't be read or "
                      "engraved” ticked.", ""]
    if ok:
        on = sheets.get("on") and sheets.get("of")
        lines += ["## Songs", "", "| Song | File | Levels | Hands from |" + (" Sheet music |" if on else ""),
                  "| --- | --- | --- | --- |" + (" --- |" if on else "")]
        for i in sorted(ok, key=lambda i: i["title"].lower()):
            hands = {"auto": "guessed", "track_names": "track names", "two_tracks": "two tracks",
                     "two_channels": "two channels", "staves": "staves"}.get(i["hand_source"], i["hand_source"])
            sheet = ""
            if on:
                n = i.get("sheets", 0)
                sheet = (" coming |" if not n and i.get("sheets_pending") else
                         f" {n} of {i['levels']}{' (more coming)' if i.get('sheets_pending') else ''} |")
            lines.append(f"| {_md(i['title'])} | `{_md(i['file'])}` | {i['levels']} | {hands} |{sheet}")
        lines.append("")
    if not items:
        lines += ["No songs yet. Put a MIDI, MusicXML or MuseScore file in `inbox/` (Add file → Upload files on GitHub).", ""]
    lines += ["To fix a title or composer, edit `library/<song>/song.json`. To remove a song, delete its `library/<song>/` folder.", ""]
    return "\n".join(lines)


def _md(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def step_summary(rep: dict) -> str:
    """The run page's summary: the same as report.md."""
    return markdown(rep)


# ------------------------------------------------------------------ command line


def commit_message(lib: Library) -> str:
    changed = [i for i in lib.items.values() if i.change]
    added = sum(i.change == "added" and i.status == "ok" for i in changed)
    updated = sum(i.change == "updated" and i.status == "ok" for i in changed)
    removed = sum(i.change == "removed" for i in changed)
    failed = sum(i.status in ("failed", "skipped") for i in changed)
    parts = [f"{n} {w}" for n, w in ((added, "added"), (updated, "updated"), (removed, "removed")) if n]
    if failed:
        parts.append(f"{failed} couldn't be read (see catalog/report.md)")
    head = "Library: " + (", ".join(parts) if parts else "nothing changed")
    return head + "\n"


def load_grade_model(path: str | None):
    """pianopipe's grade model: the installed package's own (0.2.0 and later ship one), else the file given."""
    try:
        from pianopipe import grading
    except ImportError:
        return None
    model = grading.default_model()
    if model is None and path and Path(path).exists():
        model = grading.GradeModel.load(path)
    return model


WHEEL_LINE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*@\s*(https://\S+/[^/\s]+\.whl)#sha256=([0-9a-f]{64})\s*(?:;\s*(.*?))?\s*$")
MARKER = re.compile(r"""^(sys_platform|platform_machine)\s*==\s*["']([^"']+)["']$""")


def _marker_holds(marker: str | None) -> bool | None:
    """Whether a line's environment marker fits this machine; None for one this doesn't read (pip does it then).
    Only `sys_platform == "..."` and `platform_machine == "..."`, joined with `and`."""
    import platform

    here = {"sys_platform": sys.platform, "platform_machine": platform.machine()}
    holds = True
    for part in (marker or "").split(" and ") if marker else []:
        m = MARKER.match(part.strip())
        if not m:
            return None
        holds = holds and here[m.group(1)].lower() == m.group(2).lower()
    return holds


def install_wheels(requirements: Path, site: Path) -> bool:
    """Unzips a requirements file's wheels into `site`, which then goes on PYTHONPATH. False if it can't (use pip).

    Every line has to be `name @ https://.../<file>.whl#sha256=<hash>`, optionally `; sys_platform == "linux"`
    (lines for another machine are skipped): a wheel, which pip would only unzip too (pure-Python, or native with its
    libraries inside, as manylinux and Windows wheels are), so this saves the seconds pip takes to start and to ask
    the package index. They download at the same time and each one's sha256 is checked.
    """
    import concurrent.futures
    import urllib.request

    wheels = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = WHEEL_LINE.match(line)
        holds = _marker_holds(m.group(4)) if m else None
        if holds is None:
            return False
        if holds:
            wheels.append((m.group(2), m.group(3)))

    def fetch(wheel):
        url, digest = wheel
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
        if sha256(data) != digest:
            raise ValueError(f"{url.rsplit('/', 1)[-1]} isn't the file requirements.txt names (sha256 differs)")
        return data

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(wheels))) as pool:
        files = list(pool.map(fetch, wheels))
    site.mkdir(parents=True, exist_ok=True)
    for data in files:
        with zipfile.ZipFile(_bytes_io(data)) as z:
            z.extractall(site)
    sys.path.insert(0, str(site))
    importlib.invalidate_caches()
    return True


def _run_url() -> str:
    if not os.environ.get("GITHUB_RUN_ID"):
        return ""
    return f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="inbox/ -> library/ -> catalog/")
    b.add_argument("--root", default=".")
    b.add_argument("--musescore", default=os.environ.get("MUSESCORE", ""), help="the MuseScore executable (mscore, AppRun)")
    b.add_argument("--grade-model", default=os.environ.get("GRADE_MODEL", ""),
                   help="a grade model file, for a pianopipe that doesn't ship one (0.2.0 and later do)")
    b.add_argument("--message-file", help="write the commit message here")
    b.add_argument("--retry-failed", action="store_true", help="process files that couldn't be read again")
    n = sub.add_parser("needs-musescore", help="print musescore=true|false (for $GITHUB_OUTPUT)")
    n.add_argument("--root", default=".")
    n.add_argument("--retry-failed", action="store_true")
    s = sub.add_parser("setup", help="install pianopipe into --site, then print musescore=true|false (the workflow's first step)")
    s.add_argument("--root", default=".")
    s.add_argument("--site", required=True, help="the folder to install into (put it on PYTHONPATH)")
    s.add_argument("--retry-failed", action="store_true")
    s.add_argument("--skip-install", action="store_true", help="pianopipe is installed already")
    i = sub.add_parser("install", help="install a requirements file's wheels into --site (the engraver: .github/sheet-requirements.txt)")
    i.add_argument("--root", default=".")
    i.add_argument("--site", required=True)
    i.add_argument("--requirements", required=True)
    sh = sub.add_parser("sheets", help="engrave sheet music for the levels that need it (after build)")
    sh.add_argument("--root", default=".")
    sh.add_argument("--check", action="store_true", help="only print needed=true|false (for $GITHUB_OUTPUT)")
    sh.add_argument("--minutes", type=float, default=SHEET_MINUTES, help="start no level after this; the rest is left for the next run")
    sh.add_argument("--workers", type=int, default=0, help="levels engraved at once (default: one per CPU)")
    sh.add_argument("--message-file", help="write the commit message here")
    sh.add_argument("--retry-failed", action="store_true", help="engrave levels that couldn't be engraved again")
    e = sub.add_parser("engrave-level", help=argparse.SUPPRESS)
    e.add_argument("job")
    e.add_argument("result")
    args = ap.parse_args(argv)

    if args.cmd == "engrave-level":
        result = engrave_level(json.loads(Path(args.job).read_text(encoding="utf-8")))
        Path(args.result).write_text(json.dumps(result), encoding="utf-8")
        return 0
    root = Path(args.root)

    if args.cmd in ("setup", "install"):
        reqs = root / ".github" / "requirements.txt" if args.cmd == "setup" else Path(args.requirements)
        if not getattr(args, "skip_install", False) and not install_wheels(reqs, Path(args.site)):
            subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--target", args.site,
                            "-r", str(reqs)], check=True)
            sys.path.insert(0, args.site)
            importlib.invalidate_caches()
        if args.cmd == "install":
            return 0
        args.cmd = "needs-musescore"

    if args.cmd == "sheets":
        lib = Library(root, retry_failed=args.retry_failed)
        lib.load_catalog()
        needed = lib.sheets_needed()
        if args.check or not needed:
            print(f"needed={'true' if needed else 'false'}" if args.check else "more=false")
            return 0
        missing = engraver_missing() if lib.settings.sheets else ""
        if missing:
            # Not the levels' fault: nothing is recorded, so the next run with a working engraver does them all.
            print(f"::warning::The engraver (Verovio, resvg, Pillow) couldn't be loaded ({missing}): no sheet music this run", file=sys.stderr)
            print("more=false")
            return 0
        counts = lib.engrave_sheets(args.minutes, args.workers or None)
        lib.write_reports(_run_url(), force=True)
        if args.message_file:
            Path(args.message_file).write_text(f"Library: {lib.sheet_line or 'sheet music up to date'}\n", encoding="utf-8")
        print(f"library: {lib.sheet_line or 'no sheet music to engrave'}", file=sys.stderr)
        # Another run only when this one got somewhere, so a level that never finishes can't start runs for ever.
        print(f"more={'true' if counts['left'] and counts['engraved'] + counts['failed'] else 'false'}")
        return 0

    if args.cmd == "needs-musescore":
        lib = Library(root, retry_failed=args.retry_failed)
        print(f"musescore={'true' if lib.settings.musescore and lib.needs_musescore() else 'false'}")
        return 0

    musescore = args.musescore or shutil.which("mscore") or shutil.which("mscore4") or shutil.which("musescore") or ""
    lib = Library(root, musescore=musescore or None, grade_model=load_grade_model(args.grade_model), retry_failed=args.retry_failed)
    touched = lib.ingest()
    lib.build(touched)
    rep = lib.write_reports(_run_url())
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(step_summary(rep))
    if args.message_file:
        Path(args.message_file).write_text(commit_message(lib), encoding="utf-8")
    c = rep["counts"]
    # Counts only: titles stay in the report files.
    print(f"library: {c['ok']} ready, {c['failed']} couldn't be read, {c['skipped']} skipped, {rep['needs_attention']} need attention")
    return 0


if __name__ == "__main__":
    sys.exit(main())
