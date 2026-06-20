# Duplicate Finder (Windows desktop utility)

This is a small Python utility to scan up to 6 folders, find duplicate files (by exact content and optional perceptual image similarity), preview thumbnails, and safely delete duplicates.

Quick start

1. Create a virtual environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Run the GUI:

```powershell
python -m duplicate_finder
```

Notes and design choices

- The scanner is optimized by: grouping files by file size, computing a small partial hash, then a full SHA-256 only when necessary.
- For images, when `imagehash` + `Pillow` are installed, a perceptual hash pass (`phash`) attempts to detect visually similar images even when bytes differ (different compression or sizes).
- Thumbnails are generated with Pillow and shown in the preview pane.
- Deletions use `send2trash` when available (sends to Recycle Bin). If `send2trash` is not installed the GUI will ask before performing permanent deletes.

Limitations / Critique

- GUI and scanning are intentionally pragmatic, not production hardened. Error handling is basic.
- Perceptual image clustering is O(N^2) in the number of images (simple but can be slow on very large collections). For huge libraries, a locality-sensitive hashing / bucketing approach would scale better.
- The non-image duplicate detection is byte-exact only. Detecting semantically identical files with different encodings (e.g., text with CRLF vs LF, re-encoded videos, or different archive compression levels) is out of scope for an exact-hash approach.
- Thumbnail generation for large images is done in-memory; for many thumbnails you may want to cache to disk or limit live cache size.
- The GUI updates from a background thread via a Queue; it is simple and works for typical use but could be improved (better progress feedback, cancellable operations, worker pool tuning).

If you want, I can:
- Add a CLI mode for headless scanning and CSV/JSON export.
- Replace the O(N^2) phash clustering with an approximate nearest neighbor index for speed.
- Add unit tests and a small sample dataset.
