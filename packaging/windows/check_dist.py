"""Refuse a build that names the computer it was made on: searches every file of the folder for the given
words (user name, folders, accounts), and for the private words, as UTF-8 and UTF-16 and ignoring case. Exit code 1
when one is found.

Private words are names a build must never carry, whatever build.ps1 passes. They are kept out of the repository:
one per line in refused-words.txt next to this script (not in git, see .gitignore; "#" starts a comment), and in
the environment variable VIDEO_MERGER_REFUSED_WORDS, separated by ";"."""

import os
import sys
from pathlib import Path

WORDS_FILE = Path(__file__).resolve().with_name("refused-words.txt")
WORDS_ENV = "VIDEO_MERGER_REFUSED_WORDS"


def private_words(path: Path = WORDS_FILE, environ=os.environ) -> list[str]:
    """The words in refused-words.txt (when it is there) and in VIDEO_MERGER_REFUSED_WORDS."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    words = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    return words + [word.strip() for word in environ.get(WORDS_ENV, "").split(";") if word.strip()]


def main() -> int:
    folder = Path(sys.argv[1])
    private = private_words()
    if not private:
        print(f"note: no private words ({WORDS_FILE.name} next to this script, or {WORDS_ENV}): only the given words")
    words = [w for w in [*sys.argv[2:], *private] if len(w) >= 4]
    needles = []
    for word in words:
        for variant in {word, word.replace("\\", "/"), word.lower(), word.upper()}:
            needles += [variant.encode("utf-8"), variant.encode("utf-16-le")]
    found = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        data = path.read_bytes()
        lowered = data.lower()
        for needle in needles:
            if needle in data or needle.lower() in lowered:
                found.append(f"{path.relative_to(folder)}: contains {needle.decode('utf-8', 'replace').replace(chr(0), '')!r}")
                break
    for line in found:
        print(line)
    print(f"checked {sum(1 for p in folder.rglob('*') if p.is_file())} files for {len(words)} words: "
          + ("NOT CLEAN" if found else "clean"))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
