# DuplicateFinder Build Guide (Windows)

## 1. Prerequisites
- Windows 10 or Windows 11
- Python 3.11+ in a virtual environment (`.venv`)
- Optional: Inno Setup 6+ for installer generation

## 2. Prepare assets
1. Place your icon at `assets/app.ico`
2. Edit metadata in `build_assets/version_info.txt`
3. Optional: update `installer/DuplicateFinder.iss` publisher/version

## 3. Build command (recommended)
Run from repository root:

```bat
build.bat
```

This installs dependencies and produces:
- `dist/DuplicateFinder/DuplicateFinder.exe`

## 4. Direct PyInstaller command

```bat
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean DuplicateFinder.spec
```

## 5. Regenerate executable
- Re-run `build.bat` after code or metadata changes.
- Sign executable after build (see `build_assets/signing-placeholder.txt`).

## 6. Embed icon
- Put icon at `assets/app.ico`
- Rebuild (`build.bat`)

## 7. Add/update version info
- Edit `build_assets/version_info.txt`
- Rebuild (`build.bat`)

## 8. Optional installer (Inno Setup)
1. Build app first.
2. Open `installer/DuplicateFinder.iss` in Inno Setup.
3. Compile script.
4. Output installer: `DuplicateFinder-Setup.exe`

## 9. Optional Nuitka command

```bat
.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
.venv\Scripts\python.exe -m nuitka --standalone --enable-plugin=pyqt6 --windows-console-mode=disable --windows-icon-from-ico=assets\app.ico --output-dir=dist_nuitka duplicate_finder\__main__.py
```

## 10. Distribution notes
- Share the whole `dist/DuplicateFinder/` folder for PyInstaller onedir build.
- Target machines do not need Python or pip.
