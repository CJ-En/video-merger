# Video Merger

Joins the parts of multi-part videos (for example DMM downloads that come in 2-4 parts) into one
file, without re-encoding: same quality, takes as long as copying the files.

It merges WEB-DL parts (.mkv, .mp4), and WMV parts with [AsfBin](#wmv-parts-asfbin), but not
Blu-ray/DVD Remuxes or VP9, and refuses rather than guessing. A video is only merged when no part is
missing in between, the parts match (codec, size, frame rate, audio) and the result has the right
length. A missing last part can't be noticed (unless you [check against DMM](#checking-the-parts-against-dmm)).
Otherwise you get a clear reason and your files are left alone.

It runs as a page in your browser (only your computer can use it), or in a terminal.

## What you need

- **Python 3.10 or newer**
  - Windows: [python.org](https://www.python.org/downloads/) (tick "Add python.exe to PATH").
  - Linux: usually already installed.
- **FFmpeg** (ffmpeg and ffprobe)
  - Windows: a build from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/). Put `ffmpeg.exe` and
    `ffprobe.exe` in this folder or anywhere on your PATH.
  - Linux: `sudo apt install ffmpeg`.
- **AsfBin**, only for WMV parts (Windows only, free for non-commercial use):
  [radioactivepages.com](http://www.radioactivepages.com). Put `asfbin.exe` in this folder or anywhere
  on your PATH. Without it, .wmv files are left alone.

Nothing else is needed (no `pip install`). The Windows program (`VideoMerger.exe`, see
[Windows program](#windows-program)) doesn't even need Python.

Check your setup (run these from this folder):

```
python -m video_merger doctor
```

## How to use

### In your browser

```
python -m video_merger
```

On Windows you can instead double-click `run.bat` (or `VideoMerger.exe`). A window opens and your
browser shows the page. Folders or video files dragged onto `run.bat` or `VideoMerger.exe` open the
page with them already added (`python -m video_merger gui <folders>` does the same).

1. Add the folders that hold your downloads (paste a path or "Browse…").
2. "Look for videos": each video in parts gets a row saying whether it can be merged, and why not.
3. "Merge": one video at a time, with "Stop after this one". Nothing is changed before that.

The merge options (next to the parts or all in one folder, MKV or MP4, subfolders, merge again,
delete the parts) and other copies of FFmpeg or AsfBin (Settings tab) are kept for the next start in
`%APPDATA%\VideoMerger\settings.ini` (Linux: `~/.config/video-merger/`). Closing the window (or
"Quit") stops Video Merger: a merge in progress is removed, the parts are never changed.

The page is served on `127.0.0.1` (port 8767, or a free one) and opens with a one-time key in the
address: other programs and web pages can't use it. `gui --port N` picks the port, `--no-browser`
only shows the address.

### In a terminal

Give folders or files:

```
python -m video_merger "D:\Downloads\abc00123"
python -m video_merger "D:\Downloads" --recursive --out "D:\Merged"
python -m video_merger "D:\Downloads" --dry-run
```

`run.bat` and `VideoMerger.exe` given only folders or files open the page (that is what dragging
does); with a command or an option they merge in the terminal: `VideoMerger.exe merge "D:\Downloads"`.

| Option | What it does |
|---|---|
| `-o`, `--out FOLDER` | Save merged videos here (default: next to the parts) |
| `-r`, `--recursive` | Also look in subfolders |
| `-n`, `--dry-run` | Only show what would be done |
| `--container mkv\|mp4` | Output format (default `mkv`, the safest for stream copies) |
| `--delete-parts` | Delete the parts after a merge that passed every check |
| `--overwrite` | Redo videos that were already merged |
| `--files-from LIST` | Also take the files listed in a text file (one path per line; UTF-8, or UTF-16 as Windows PowerShell 5.1 writes it) |
| `--dmm-parts CSV` | Only merge videos whose parts match DMM's own part list (see below) |
| `-v`, `--verbose` | Show every part and more detail |
| `--ffmpeg`, `--ffprobe PATH` | Use these programs (a file or a folder) |
| `--asfbin PATH` | Use this AsfBin (a file or a folder), for WMV parts |

The terminal doesn't read the page's settings file. `python -m video_merger doctor` shows which
programs are found.

## How parts are found

1. **DMM files**: DMM writes the content id and part number into each downloaded file's comment tag
   (`{"file_id":"abc00123","part":"2"}`). Parts are matched and
   ordered by that, whatever the files are called and whichever folder they are in. The merged
   file is named after the content id (`abc00123.mkv`) and keeps the content id in its comment.
   DMM's own suffixes in the names (`abc00123hhb1`, `hhbs2`, `mhb1`, `...2d_v1_drm_a_4k1`) don't matter.
   A file whose tag has no part number is a whole video, whatever it is called (`abc00123_2.mkv`).
2. **Other files**: by name, within one folder: `name-pt1`, `name part 2`, `name part ii`, `name.cd3`,
   `name_4` (also after Japanese names), and after a DVD id `ABC-123-1` or `ABC-123-A`. A number
   straight after the dash is the video id (`ABC-012`), not a part.
3. **Looser names**, only when part 1 and at least another part of the same name are in the folder
   (one file alone is a whole video): `film006-1`, `Name 2`, `Name - 3`, `FC2-PPV-1234567-2`, and a
   roman number or a part in brackets anywhere in the name: `[PPV1007] Name (ii)(Actress)`,
   `Name [Part 2] 720p` (merged as `[PPV1007] Name (Actress)`, `Name 720p`). A bare `(2)` is not a
   part: Windows and browsers name second copies of a file that way. Check these rows before merging:
   `Episode 1` and `Episode 2` would be taken for parts too. On the page, open a wrong row and press
   "Leave out" (kept until the program closes, also after "Look again"; the files are not changed).

Content ids are kept exactly as written: `sone096` and `sone00096` are different videos and are
never merged together (some ids that differ only in zeros really are different videos).

Parts are ordered by number (part 2 before part 10). The merged file gets a chapter
("Part 1", "Part 2", ...) at each join, and keeps every audio and subtitle track.

## WMV parts (AsfBin)

With AsfBin found (`--asfbin`, the Settings tab, next to the program, or PATH), .wmv files are parts
too, found the same way (DMM's comment tag, else the names). They are joined with AsfBin, never
re-encoded:

```
asfbin -i <part 1> -i <part 2> ... -o <name>.partial.wmv -y -m <markers>
```

- The result is always `<name>.wmv`, whatever `--container` says (WMV stays WMV).
- Only Windows Media video (WMV1, WMV2, WMV3, VC-1) with WMA sound is joined; other codecs inside a
  .wmv are refused, and so are parts that mix .wmv with .mkv/.mp4 ("the parts are in different
  formats (WMV and MP4)").
- Each join gets a marker "Part N" (a list next to the output while AsfBin runs, then removed).
- Then the same checks as every merge (length, size, every stream) before the `.partial.wmv` is
  renamed, and the parts are only deleted (when asked) after that. Progress is the size written so
  far; AsfBin is stopped when nothing changes for 10 minutes.
- AsfBin reads its command line in Windows' language code page: names with other letters (e.g.
  Japanese on a Western Windows) are given in their short 8.3 form, or the video isn't merged
  ("rename the folder or the files with plain letters and digits").

## Checking the parts against DMM

The part numbers show gaps (part 2 of 1, 3) but not a missing last part. With `--dmm-parts`, each
video is checked against a list of DMM's parts: a CSV file with one line per part (or one per
video), with these columns (others are ignored):

| Column | Meaning |
|---|---|
| `ids` | The video's content ids, separated by spaces (`172xrw344 172xrw00344`). Lines with the same first id are one video. |
| `page_id`, `file_id` | Optional: more ids the video is found under (its DMM page, the content id in the parts' tag). |
| `parts` | How many parts DMM has. Empty when unknown. |
| `rates` | Optional: the part count at each bitrate (`3000:3 300:2`), shown when they differ. |
| `part`, `duration_s` | Optional: a part's number and its length in seconds. |
| `error` | Why the part count (or that part's length) is unknown. |

A video is then only merged when it has exactly DMM's parts, numbered 1 to N, and each part is as
long as DMM's (within 2 seconds or 0.5%, when the list has the lengths). Videos the list doesn't
cover are not merged.

```
python -m video_merger "/data/dmm" --dmm-parts parts.csv --dry-run
```

## When a video is not merged

| Message | Meaning |
|---|---|
| `missing part(s) 2` | Not all parts are there. Download the missing one. |
| `part 3 was found 2 times` | The same part is in two places. Remove one copy. |
| `the parts don't match (...)` | The parts differ (size, codec, frame rate, audio), so they can't be joined without re-encoding. HEVC parts must also have been encoded with the same settings ("encoded with other settings"): joined, part 2 would play with part 1's settings, as a broken picture. |
| `VP9 video: the parts stay separate files` | DMM's 4K VP9 parts are left as separate files. |
| `... Remuxes and rips are not merged`, `... audio looks like a Blu-ray/DVD Remux` | Only WEB-DL parts are merged (H.264/HEVC video with AAC audio, as DMM delivers them). Files with Remux/BluRay/BD/DVD/ISO in the name or folder name, disc audio (LPCM, TrueHD, DTS, AC-3) or disc subtitles are left alone. |
| `Ignored 2 .wmv file(s)` | Other formats (.avi, .ts, .m2ts, .vob, .iso, ...) are never merged; .wmv files only with AsfBin. |
| `the parts are in different formats (WMV and MP4)` | Some parts are .wmv and others not: they can't be joined without re-encoding. |
| `part 2 is MP4 and part 1 is Matroska: remux them to the same kind first` | The parts are different kinds of file inside (what ffprobe finds, whatever the extension): ffmpeg can't join them as they are. Remux them to the same kind (e.g. MKVToolNix, or `ffmpeg -i part.mp4 -c copy part.mkv`), which doesn't re-encode. |
| `h264 video inside a .wmv file` | AsfBin only joins WMV/VC-1 video with WMA sound. |
| `already merged` | The output file is there and is this video: the same content id in its DMM tag, or for parts found by name the same length and size as the parts together. Use `--overwrite` to redo it. |
| `... is already there, but it is another video` | A file with the merged video's name is there, but it is another video (another content id in its tag, no tag, another length or size) or it can't be read. It is never skipped as merged and never replaced, also with `--overwrite`: move or rename it, or save to another folder (`--out`). |
| `DMM has 3 part(s), found part(s) 1, 2` | With `--dmm-parts`: DMM sells this video in more (or fewer) parts than are here. |
| `part 2 is 0:40:00 long, DMM's part 2 is 0:50:00` | With `--dmm-parts`: a part is cut short (or another file). |
| `not in the DMM parts list`, `no DMM comment tag` | With `--dmm-parts`: DMM's list doesn't cover this video, so it isn't merged. |
| `only part 1 of abc00123 is here` | With `--dmm-parts`: a single part file of a video DMM has in several parts. |
| `FAILED: ...` | ffmpeg or AsfBin failed, or the result didn't pass the checks (length within 2 s or 0.5% of the parts' total, size within 1% of what the parts take in that kind of file, every stream kept: an MKV of MP4 parts is a little smaller than the parts, an MP4 of MKV parts a little bigger). No output is kept and the parts are not changed. A file of the output's name that appeared after the parts were checked (or the one to redo, changed since) is never replaced: `... is there now ..., so it is not replaced`. |

A warning that sound and picture lengths differ means the sound could be slightly out of sync
after the join. Check the merged file around the chapter marks.

Unfinished output is written as `<name>.partial.mkv` and only renamed when it passed every check
and is on the disk (not only in the system's cache), so a power cut can't leave a damaged file
under the finished name. If you stop the program (Ctrl+C; for the page also Quit or closing its window), that file is
removed and ffmpeg or AsfBin is stopped with it.

## Exit codes and log

- **Exit codes**: `0` all good, `1` something was not merged, `2` setup problem (FFmpeg missing),
  `130` stopped.
- **Log file**:
  - Windows: `%LOCALAPPDATA%\VideoMerger\Logs\video-merger.log`
  - Linux: `~/.local/state/video-merger/`
  - macOS: `~/Library/Logs/VideoMerger/`

## Windows program

`VideoMerger.exe` is the same tool built with PyInstaller (no Python needed). Put `ffmpeg.exe`,
`ffprobe.exe` and `asfbin.exe` next to it; they are never part of the download.

It isn't signed, so Windows may warn that it is from an unknown publisher ("More info" -> "Run
anyway"). With Windows' Smart App Control on, it is blocked completely, without "Run anyway": then
use the Python version (`python -m video_merger`, see [How to use](#how-to-use)).

Build it on Windows:

```
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

It makes `dist\VideoMerger-<version>-windows-x64.zip` and updates `dist\VideoMerger\` (only the
build's own files are replaced there). The build refuses to finish when a file in it names the
computer it was made on (user name, folders, git account), or a word of your own: one per line in
`packaging\windows\refused-words.txt` (not in git), or in `VIDEO_MERGER_REFUSED_WORDS` separated by
`;`. `packaging\windows\make_icon.py` draws the icon from the page's mark.
`packaging\windows\notices.py` writes `THIRD-PARTY-NOTICES.txt` from the build: the Python, OpenSSL and
PyInstaller versions in it and their licenses; a library in the build that no notice covers stops it.

## Development

```
python -m unittest discover -s tests -t .
```

The end-to-end tests make small clips with FFmpeg and are skipped when FFmpeg is not installed. AsfBin
is never run by the tests: `tests/fake_asfbin.py` plays it.

## Changes in 1.0.1

- Low-bitrate MP4 parts (for example DMM's 300 kbit/s downloads) are merged into MKV. 1.0.0 refused
  them ("the merged file is 0.17 GB, but the parts add up to 0.17 GB"): MKV keeps the same video in
  a little less room than MP4, and the size check didn't allow for that. It does now, both ways,
  and still refuses a merged file that lost even 2% of its picture and sound.
- Sizes in messages are in MB below 1 GB, with enough decimals to tell them apart.
- The Settings tab and the guides no longer say that every part is checked: a part missing in
  between is found, a missing last part can't be.
- The Windows guide says what to do when Smart App Control blocks the program.

## License

MIT (`LICENSE`). The Windows program also carries Python, OpenSSL and PyInstaller's starter, each under
its own license (`THIRD-PARTY-NOTICES.txt` in the download). FFmpeg and AsfBin are not part of Video
Merger and are never included.
