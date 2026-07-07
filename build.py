from __future__ import annotations

import os
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent
ICON = ROOT / "assets" / "app.ico"
SPEC = ROOT / "DuplicateFinder.spec"


def main() -> None:
    if not SPEC.exists():
        raise SystemExit("Missing DuplicateFinder.spec")

    args = [
        "--noconfirm",
        "--clean",
        str(SPEC),
    ]

    if not ICON.exists():
        print("[build] assets/app.ico not found - building without custom icon")

    os.chdir(ROOT)
    PyInstaller.__main__.run(args)
    print("[build] done: dist/DuplicateFinder/DuplicateFinder.exe")


if __name__ == "__main__":
    main()
