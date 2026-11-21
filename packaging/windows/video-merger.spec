# PyInstaller: Video Merger as a Windows program in one folder (build.ps1 runs it). FFmpeg and AsfBin are never part
# of it: users put ffmpeg.exe, ffprobe.exe and asfbin.exe next to VideoMerger.exe themselves.
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]
HERE = ROOT / "packaging" / "windows"
ICON = str(HERE / "video-merger.ico")  # made by make_icon.py

main = Analysis(
    [str(HERE / "video_merger_app.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "video_merger" / "web"), "video_merger/web")],
    excludes=["tkinter", "_tkinter"],
    noarchive=False,
)
exe = EXE(
    PYZ(main.pure),
    main.scripts,
    [],
    exclude_binaries=True,
    name="VideoMerger",
    console=True,  # the window shows the page's address; closing it stops Video Merger
    icon=ICON,
    upx=False,
)
coll = COLLECT(exe, main.binaries, main.datas, name="VideoMerger", upx=False)
