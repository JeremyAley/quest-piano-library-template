# My piano library (Quest Piano Trainer)

Your own songs for [Quest Piano Trainer](https://github.com/JeremyAley/pianopipe), kept in a repository that only you
can see. Put MIDI, MusicXML or MuseScore files in `inbox/`; a GitHub Action turns each one into the app's four
levels (Melody, Melody + bass, Simplified, Full) with finger numbers, and the songs appear in the app's library under
**My library**.

## Keep it private

**Choose Private when you create the repository.** This library is for music you have the right to use: your own
arrangements, files you bought for your own practice, public-domain pieces. Don't make a library with copyrighted
songs public, and don't share it. The app has no sharing features for libraries, and your files only ever go between
GitHub and your own devices.

## Set up (5 minutes, on your phone or PC)

1. **Use this template → Create a new repository.** Pick a name (for example `piano-library`) and choose
   **Private**.
2. **Install the Quest Piano Trainer app on it** (the headset's My library panel shows the link,
   github.com/apps/&lt;app&gt;/installations/new). Choose **Only select repositories** and pick your library. The app
   can then read that one repository, add files to its `inbox/` and see its Action's progress, and nothing else.
3. In the headset: **Songs → Mine → My library → Sign in with GitHub.** The panel shows a code. On your phone, open
   **github.com/login/device**, type the code and approve.
4. Pick your library from the list (or type owner/name on the panel). It shows up as **My library**.

## Add songs

- **From the headset:** My library → **Wi-Fi drop**. Open the address it shows on a phone or PC on the same Wi-Fi, enter
  the 6-digit code and drag files in. They're added to `inbox/` in one commit, and play on the headset straight away
  (Full level) while GitHub makes the rest.
- **On GitHub (phone or PC):** open `inbox/`, then **Add file → Upload files** and **Commit changes**.
- **With git:** copy files into `inbox/`, commit and push.

A run takes about 10 seconds, and the song is in the app's library about 20 seconds after you drop it (the Actions
tab shows each run). Then:

- each file has moved to `library/<song>/` (your file is kept there as `source.<ext>`);
- `catalog/` has the song's levels and `catalog.json`, which the app reads;
- **`catalog/report.md`** says what happened to every file: what's ready, what couldn't be read and why, and where
  the hands were guessed.

**What works best:** MusicXML (`.musicxml`, `.mxl`) or MuseScore files, whose two staves say which hand plays what.
MusicXML is read directly; MuseScore files are converted with MuseScore first, which adds about half a minute. A MIDI file with
separate right- and left-hand tracks is as good. A single-track MIDI works too, but the hands are guessed.

**What doesn't work yet:** PDFs and pictures of sheet music, and recordings (MP3, WAV). Export MusicXML or MIDI from
the app that made the file, or download that version instead.

## Change things

- **Title or composer:** edit `library/<song>/song.json`. The next run updates the catalog.
- **Remove a song:** delete its `library/<song>/` folder.
- **Replace a file:** put a new version with the same file name in `inbox/`.
- **Settings** (library name, finger numbers, MuseScore): `library.toml`.
- **A newer pipeline:** change pianopipe's version and sha256 in `.github/requirements.txt` (the release page shows
  both); every song is processed again.

## Costs

Private repositories get 2,000 free Action minutes a month on GitHub Free. A run takes well under a minute (GitHub
counts each as one), so well over a thousand uploads a month fit. Runs only happen when files in `inbox/`, `library/` or `library.toml` change, and only changed
songs are processed again.

## Without the headset

Everything except playing works from a phone or PC: upload to `inbox/`, read `catalog/report.md`, edit `song.json`.
To run the same build on a PC (Python 3.12):

```
pip install -r .github/requirements.txt
python scripts/library.py build
```

## What's in here

```
inbox/                     drop files here
library/<song>/            source.<ext> (your file) and song.json (title, composer)
catalog/catalog.json       the song list the app reads
catalog/songs/<song>/      level1.mid .. level4.mid
catalog/report.md          what happened to each file (report.json: the same for the app)
catalog/build.lock         which songs are up to date (don't edit)
library.toml               settings
scripts/library.py         the build (uses pianopipe, Apache-2.0)
.github/workflows/library.yml
```

The template's own files (scripts, workflow, this README) are under the Apache-2.0 licence, like pianopipe. Your
songs are yours.
