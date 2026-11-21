Video Merger for Windows
========================

Joins the parts of multi-part videos (for example DMM downloads in 2-4 parts) into one file,
without re-encoding: same quality, about as fast as copying the files.

Before the first start
----------------------
1. FFmpeg (needed): https://www.gyan.dev/ffmpeg/builds/
   Take the "release essentials" build (zip). ffmpeg.exe and ffprobe.exe are in its bin folder.
   (FFmpeg for other systems: https://ffmpeg.org/download.html)
2. AsfBin (only for WMV parts): http://www.radioactivepages.com
   Windows only, free for non-commercial use. Without it, .wmv files are left alone.

Put ffmpeg.exe, ffprobe.exe and asfbin.exe next to VideoMerger.exe (or in a folder named "bin"
there), or anywhere on PATH, or give their places on the page: Settings -> "Use other copies".
They are NOT included in this download.

Start
-----
Start VideoMerger.exe. A window opens and your browser shows Video Merger's page (only this
computer can use it). You can also drag folders or video files onto VideoMerger.exe: the page
opens with them added.

Add the folders that hold your downloads, press "Look for videos", check the list, then
"Merge". Nothing is changed before you press "Merge".

Keep the window open while you merge. Closing it (or "Quit" on the page) stops Video Merger and
every program it started: a merge in progress is removed, the parts are never changed.

What gets merged
----------------
- WEB-DL parts (.mkv, .mp4: H.264 or HEVC video with AAC sound): joined by FFmpeg into one
  .mkv (or .mp4), with a chapter at each join.
- WMV parts (.wmv: WMV or VC-1 video with WMA sound): joined by AsfBin into one .wmv, with a
  marker at each join. Always a .wmv, whatever file type is chosen.
- Never re-encoded. A video is merged only when no part is missing in between, the parts
  match, and the result has the right length and size and every track. A missing last part
  can't be noticed. Left alone: VP9 4K parts, Blu-ray/DVD Remuxes and rips, parts in different
  formats, AVI, TS, VOB, ISO.
- The parts are deleted only when you tick "Delete the parts after a merge", and only after
  their merged file passed every check.
- Parts are found by DMM's tag in the files, or by their names in one folder: name-pt1,
  name part 2, name_3, and (when part 1 and another part are both there) name-1, name 2,
  Name (ii) more. Look at the rows before merging: "Episode 1" and "Episode 2" look like parts.
  Open a wrong row and press "Leave out": it is not merged, its files are not changed.

Files
-----
The log is in %LOCALAPPDATA%\VideoMerger\Logs, the page's settings in
%APPDATA%\VideoMerger\settings.ini.

LICENSE.txt is Video Merger's license (MIT). THIRD-PARTY-NOTICES.txt lists what the
program carries so it runs without Python installed (Python, OpenSSL, PyInstaller's
starter) with their licenses.

Terminal use: VideoMerger.exe --help (e.g. VideoMerger.exe merge D:\Downloads --dry-run).

Windows may warn that the program is from an unknown publisher (it isn't signed):
"More info" -> "Run anyway". With Windows' Smart App Control on, the program is blocked
completely, without "Run anyway". Then use the Python version instead: python -m video_merger
(see README.md in Video Merger's source code).
