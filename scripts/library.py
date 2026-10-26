#!/usr/bin/env python3
"""Turn the files dropped into inbox/ into the catalog the Quest Piano Trainer app reads.

.github/workflows/library.yml runs this on every push that touches inbox/, library/ or library.toml. It runs the
same way on a PC:

    pip install -r .github/requirements.txt
    python scripts/library.py build [--musescore PATH] [--grade-model PATH]

Folders:
    inbox/<file>         anything dropped in: MIDI (.mid .midi .kar), MusicXML (.musicxml .xml .mxl), MuseScore (.mscz .mscx)
    library/<id>/        one folder per song: source.<ext> (the file as it came) and song.json (title, composer, ...)
    catalog/             what the app reads: catalog.json, songs/<id>/level1..4.mid, report.json, report.md

Rules:
    - A file in inbox/ moves to library/<id>/ and becomes a song. Dropping a file with the same name again replaces it;
      the same file under another name is noticed and left out.
    - Only songs whose source, song.json, settings or pianopipe version changed are processed again (catalog/build.lock),
      so unchanged level files keep their sha256 and the app downloads nothing new.
    - One bad file never stops the build: it stays in library/<id>/, the report says why, the rest still updates.
      Only a crash in this script turns the run red.
    - Deleting a library/<id>/ folder removes the song from the catalog.
    - MusicXML and MuseScore files go through the MuseScore command line (mscore -o song.mid) until pianopipe reads
      MusicXML itself (pianopipe.musicxml); then MusicXML is read directly and MuseScore is only needed for .mscz.

No song titles are printed outside report.md / report.json and the run summary, which stay in this (private) repo.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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

    @classmethod
    def load(cls, root: Path) -> "Settings":
        path = root / "library.toml"
        s = cls()
        if path.exists():
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            s.name = str(data.get("name", s.name)) or s.name
            s.fingering = bool(data.get("fingering", s.fingering))
            s.musescore = bool(data.get("musescore", s.musescore))
        return s

    def key(self) -> str:
        return f"fingering={self.fingering}"


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


def musicxml_info(data: bytes, ext: str) -> tuple[str, str]:
    """(title, composer) from a MusicXML or MXL file's header, or ("", "")."""
    try:
        if ext == ".mxl" or data[:2] == b"PK":
            with zipfile.ZipFile(_bytes_io(data)) as z:
                inner = _mxl_root(z)
                if inner is None:
                    return "", ""
                data = z.read(inner)
        root = ElementTree.fromstring(data)
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError):
        return "", ""
    title = (root.findtext("work/work-title") or root.findtext("movement-title") or "").strip()
    composer = ""
    for c in root.findall("identification/creator"):
        if c.get("type") == "composer" and (c.text or "").strip():
            composer = c.text.strip()
            break
    return title, composer


def _bytes_io(data: bytes):
    import io

    return io.BytesIO(data)


def _mxl_root(z: zipfile.ZipFile) -> str | None:
    """The score inside an .mxl: META-INF/container.xml's rootfile, else the first .xml/.musicxml that isn't META-INF."""
    try:
        container = ElementTree.fromstring(z.read("META-INF/container.xml"))
        for el in container.iter():
            if el.tag.endswith("rootfile") and el.get("full-path"):
                return el.get("full-path")
    except KeyError:
        pass
    for n in z.namelist():
        if not n.startswith("META-INF") and n.lower().endswith((".xml", ".musicxml")):
            return n
    return None


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

    def to_json(self) -> dict:
        return {
            "id": self.id, "file": self.file, "title": self.title, "status": self.status, "reasons": self.reasons,
            "warnings": self.warnings, "hand_source": self.hand_source, "levels": self.levels, "change": self.change,
            "added": self.added,
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
        self.inbox_notes: list[str] = []  # inbox files that didn't become songs (duplicates, too big), for the report
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
        for path in sorted(p for p in self.inbox.rglob("*") if p.is_file()):
            name = path.name
            if name.lower() in IGNORED or name.startswith("."):
                continue
            size = path.stat().st_size
            if size > MAX_BYTES:
                self.inbox_notes.append(f"{name}: left out, it's {size / 1e6:.0f} MB (the limit is {MAX_BYTES // 2**20} MB)")
                path.unlink()
                continue
            data = path.read_bytes()
            digest = sha256(data)
            ext = path.suffix.lower()
            if digest in by_sha and by_name.get(name.lower()) != by_sha[digest]:
                other = by_sha[digest]
                self.inbox_notes.append(f"{name}: left out, it's the same file as {self.song_json(other).get('title') or other}")
                path.unlink()
                continue
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
            title, composer = musicxml_info(data, ext) if ext in MUSICXML else ("", "")
            # A replaced file keeps its song's title (it may have been fixed by hand in song.json).
            meta["title"] = meta.get("title") or title or title_from_name(name)
            if composer and not meta.get("composer"):
                meta["composer"] = composer
            meta.setdefault("composer", "")
            meta["original_name"] = name
            meta["format"] = format_of(ext)
            meta["source_sha256"] = digest
            meta.setdefault("added", self.now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            meta["updated"] = self.now.strftime("%Y-%m-%dT%H:%M:%SZ")
            (folder / "song.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
            by_name[name.lower()] = sid
            by_sha[digest] = sid
            touched.append(sid)
        return touched

    # ---- step 2: library/<id>/ -> catalog/

    def needs_musescore(self) -> bool:
        """Whether this run will convert anything with MuseScore (the workflow only downloads it then)."""
        reads_xml = pianopipe_reads_musicxml()
        exts = [p.suffix.lower() for p in self.inbox.rglob("*") if p.is_file()] if self.inbox.exists() else []
        for d in sorted(self.songs_dir.glob("*/")) if self.songs_dir.exists() else []:
            src = self.source_of(d.name)
            prev = self.lock["songs"].get(d.name, {})
            if src is not None and (self._key(d.name, src) != prev.get("key") or (self.retry_failed and prev.get("status") == "failed")):
                exts.append(src.suffix.lower())
        return any(e in MUSESCORE or (e in MUSICXML and not reads_xml) for e in exts)

    def _key(self, sid: str, src: Path) -> str:
        # song.json by content, so line endings or spacing (a Windows checkout, an edit on github.com) don't matter.
        meta = json.dumps(self.song_json(sid), sort_keys=True).encode()
        # A different converter (MuseScore version) is a reason to try MusicXML / MuseScore files again.
        tool = os.environ.get("CONVERTER", "") if src.suffix.lower() in MUSICXML + MUSESCORE else ""
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
            prev = lock_songs.get(sid, {})
            if src is None:
                item.status, item.reasons = "failed", ["the folder has no source file (source.mid, source.musicxml, ...)"]
                self.items[sid] = item
                lock_songs[sid] = {"key": "", **_outcome(item)}
                continue
            key = self._key(sid, src)
            retry = self.retry_failed and prev.get("status") == "failed"
            reuse = (prev.get("key") == key and sid not in touched and not retry and
                     (prev.get("status") != "ok" or (sid in old_entries and _levels_present(old_entries[sid], self.catalog_dir))))
            if reuse:
                item.status, item.reasons, item.warnings = prev.get("status", "ok"), prev.get("reasons", []), prev.get("warnings", [])
                item.hand_source, item.levels = prev.get("hand_source", ""), prev.get("levels", 0)
                if item.status == "ok":
                    entries[sid] = old_entries[sid]
            else:
                item.change = "updated" if sid in old_entries or prev else "added"
                shutil.rmtree(songs_out / sid, ignore_errors=True)
                entry = self._process(sid, src, meta, item)
                if entry is not None:
                    entries[sid] = entry
            self.items[sid] = item
            lock_songs[sid] = {"key": key, **_outcome(item)}
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
        self.lock["pianopipe"] = pianopipe_version()
        self.lock["settings"] = self.settings.key()
        self.lock_path.write_text(json.dumps(self.lock, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")

    def _process(self, sid: str, src: Path, meta: dict, item: Item):
        from pianopipe.process import process_file

        ext = src.suffix.lower()
        if ext not in SUPPORTED:
            item.status = "skipped"
            item.reasons = [NOT_MUSIC.get(ext, f"“{ext or 'no extension'}” isn't a music file this library reads "
                                               "(MIDI, MusicXML, MXL or MuseScore)")]
            return None
        work = Path(tempfile.mkdtemp(prefix="library-"))
        try:
            midi = src
            if ext in MUSESCORE or (ext in MUSICXML and not pianopipe_reads_musicxml()):
                midi, error = self._convert(src, work)
                if midi is None:
                    item.status, item.reasons = "failed", [error]
                    return None
            fields = {k: meta[k] for k in ("title", "full_title", "composer", "era", "character") if meta.get(k)}
            fields["id"] = sid
            fields["tags"] = [t for t in meta.get("tags", []) if isinstance(t, str)]
            kwargs = {"fingering": self.settings.fingering}
            if self.grade_model is not None:
                kwargs["grade_model"] = self.grade_model
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
        if entry.warnings:
            shown = entry.warnings[:5]
            more = len(entry.warnings) - len(shown)
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

    # ---- step 3: reports

    def report(self, run_url: str = "") -> dict:
        items = sorted(self.items.values(), key=lambda i: (i.status == "ok", i.title.lower()))
        counts = {s: sum(i.status == s for i in items) for s in ("ok", "skipped", "failed")}
        attention = [i for i in items if i.status in ("skipped", "failed") or i.warnings]
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
        }

    def changed(self) -> bool:
        return bool(self.inbox_notes) or any(i.change for i in self.items.values())

    def write_reports(self, run_url: str = "") -> dict:
        rep = self.report(run_url)
        if not self.changed() and (self.catalog_dir / "report.json").exists():
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
    items = rep["items"]
    changed = [i for i in items if i["change"]]
    if changed or rep["removed"] or rep["inbox"]:
        lines += ["## This run", ""]
        for i in changed:
            what = {"ok": "ready", "failed": "couldn't be read", "skipped": "skipped"}[i["status"]]
            lines.append(f"- {i['change'].capitalize()}: **{_md(i['title'])}** (`{_md(i['file'])}`): {what}")
        for sid in rep["removed"]:
            lines.append(f"- Removed: `{sid}`")
        for note in rep["inbox"]:
            lines.append(f"- {_md(note)}")
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
    if ok:
        lines += ["## Songs", "", "| Song | File | Levels | Hands from |", "| --- | --- | --- | --- |"]
        for i in sorted(ok, key=lambda i: i["title"].lower()):
            hands = {"auto": "guessed", "track_names": "track names", "two_tracks": "two tracks",
                     "two_channels": "two channels", "staves": "staves"}.get(i["hand_source"], i["hand_source"])
            lines.append(f"| {_md(i['title'])} | `{_md(i['file'])}` | {i['levels']} | {hands} |")
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
    """pianopipe's grade model: the installed package's own (a source checkout), else the file given."""
    try:
        from pianopipe import grading
    except ImportError:
        return None
    model = grading.default_model()
    if model is None and path and Path(path).exists():
        model = grading.GradeModel.load(path)
    return model


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="inbox/ -> library/ -> catalog/")
    b.add_argument("--root", default=".")
    b.add_argument("--musescore", default=os.environ.get("MUSESCORE", ""), help="the MuseScore executable (mscore, AppRun)")
    b.add_argument("--grade-model", default=os.environ.get("GRADE_MODEL", ""), help="pianopipe's data/grade_model.json")
    b.add_argument("--message-file", help="write the commit message here")
    b.add_argument("--retry-failed", action="store_true", help="process files that couldn't be read again")
    n = sub.add_parser("needs-musescore", help="print musescore=true|false (for $GITHUB_OUTPUT)")
    n.add_argument("--root", default=".")
    n.add_argument("--retry-failed", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.root)

    if args.cmd == "needs-musescore":
        lib = Library(root, retry_failed=args.retry_failed)
        print(f"musescore={'true' if lib.settings.musescore and lib.needs_musescore() else 'false'}")
        return 0

    musescore = args.musescore or shutil.which("mscore") or shutil.which("mscore4") or shutil.which("musescore") or ""
    lib = Library(root, musescore=musescore or None, grade_model=load_grade_model(args.grade_model), retry_failed=args.retry_failed)
    touched = lib.ingest()
    lib.build(touched)
    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    rep = lib.write_reports(run_url)
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
