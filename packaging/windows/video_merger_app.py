"""Start of the Windows program (VideoMerger.exe): without arguments, or with folders and files dragged onto it, the
browser page; with a command (merge, doctor) the terminal. Not named video_merger.py, which would hide the package of
the same name."""

import sys

from video_merger.cli import main

if __name__ == "__main__":
    sys.exit(main(launcher=True))
