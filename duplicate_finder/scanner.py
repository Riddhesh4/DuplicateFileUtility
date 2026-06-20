"""Core scanning utilities for Duplicate Finder.

Features:
- Walk up to 6 user-supplied paths and collect files
- Fast filtering by file size, then partial-hash, then full SHA256
- Optional perceptual image hashing (imagehash) to detect visually
  identical images even when file bytes differ
"""
from __future__ import annotations

import os
import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Callable, Dict, Any

try:
    from PIL import Image
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

try:
    import imagehash
    IMAGEHASH_AVAILABLE = True
except Exception:
    IMAGEHASH_AVAILABLE = False

CHUNK_SIZE = 8 * 1024 * 1024
PARTIAL_READ = 64 * 1024

# Hamming distance threshold for perceptual image hashing (phash)
# Increase if you expect more lossy transformations to still be considered duplicates
PHASH_HAMMING_THRESHOLD = 10

logger = logging.getLogger("duplicate_finder.scanner")


@dataclass
class FileEntry:
    path: str
    size: int
    mtime: float
    name: str
    ext: str
    width: Optional[int] = None
    height: Optional[int] = None
    partial_hash: Optional[str] = None
    full_hash: Optional[str] = None
    phash: Optional[object] = None  # imagehash.ImageHash when available


def _is_image_file(path: str) -> bool:
    if not PIL_AVAILABLE:
        return False
    try:
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def _compute_partial_hash(path: str) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            h.update(fh.read(PARTIAL_READ))
        return h.hexdigest()
    except Exception:
        logger.exception("Partial hash failed for %s", path)
        return None


def _compute_full_hash(path: str) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        logger.exception("Full hash failed for %s", path)
        return None


def _compute_image_info(entry: FileEntry) -> None:
    if not PIL_AVAILABLE:
        return
    try:
        with Image.open(entry.path) as im:
            entry.width, entry.height = im.size
            if IMAGEHASH_AVAILABLE:
                try:
                    # compute perceptual hash (phash)
                    entry.phash = imagehash.phash(im)
                except Exception:
                    logger.exception("phash failed for %s", entry.path)
    except Exception:
        # Image couldn't be opened/verified
        pass


def _suggest_best(entries: List[FileEntry]) -> int:
    # Prefer higher visual quality for images (resolution), otherwise larger file size
    if not entries:
        return 0
    image_candidates = [e for e in entries if e.width]
    if image_candidates:
        best = max(image_candidates, key=lambda e: ((e.width or 0) * (e.height or 0), e.size, e.mtime))
    else:
        best = max(entries, key=lambda e: (e.size, e.mtime))
    for i, e in enumerate(entries):
        if e.path == best.path:
            return i
    return 0


def _collect_files(paths: List[str], max_paths: int = 6) -> List[FileEntry]:
    if len(paths) > max_paths:
        raise ValueError(f"Only up to {max_paths} paths are accepted")
    files: List[FileEntry] = []
    seen = set()
    for base in paths:
        for root, dirs, filenames in os.walk(base):
            for fn in filenames:
                p = os.path.join(root, fn)
                if p in seen:
                    continue
                seen.add(p)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                files.append(FileEntry(path=p, size=st.st_size, mtime=st.st_mtime, name=fn, ext=os.path.splitext(fn)[1].lower()))
    return files


def find_duplicates(paths: List[str], workers: Optional[int] = 4, progress_callback: Optional[Callable[[Any], None]] = None) -> List[dict]:
    """Scan given paths and return list of duplicate groups.

    Each group is a dict: {'type': 'exact'|'perceptual', 'hash': str|None, 'files': List[FileEntry], 'suggested': int}
    """
    files = _collect_files(paths)

    # allow automatic worker count based on CPU cores if None
    if workers is None:
        try:
            import os as _os
            workers = max(2, _os.cpu_count() or 4)
        except Exception:
            workers = 4

    # collect image info (size + phash) concurrently (cheap compared to full hashing)
    if PIL_AVAILABLE:
        if progress_callback:
            progress_callback({"type": "status", "text": "Collecting image metadata..."})
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_compute_image_info, f) for f in files]
            for fut in as_completed(futures):
                # no-op but keep progress responsive
                if progress_callback:
                    progress_callback({"type": "status", "text": "Collecting image metadata..."})

    # group by file size (quick filter)
    size_map = defaultdict(list)
    for f in files:
        size_map[f.size].append(f)

    duplicates: List[dict] = []

    # Step 1: within each size group do partial->full hashing
    if progress_callback:
        progress_callback({"type": "status", "text": "Running size/partial/full-hash pass..."})

    # For determinism in progress reporting, compute total candidate files for hashing
    candidate_groups = [g for g in size_map.values() if len(g) > 1]
    total_partial = sum(len(g) for g in candidate_groups)
    if progress_callback:
        progress_callback({"type": "total", "total": total_partial})

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for size, group in size_map.items():
            if len(group) < 2:
                continue
            # partial hashes
            partial_futs = {ex.submit(_compute_partial_hash, e.path): e for e in group}
            for fut in as_completed(partial_futs):
                entry = partial_futs[fut]
                try:
                    entry.partial_hash = fut.result()
                except Exception:
                    entry.partial_hash = None
                if progress_callback:
                    progress_callback({"type": "file", "stage": "partial", "path": entry.path})

            # group by partial hash
            partial_groups = defaultdict(list)
            for e in group:
                partial_groups[e.partial_hash].append(e)

            for pgroup in partial_groups.values():
                if len(pgroup) < 2:
                    continue
                full_futs = {ex.submit(_compute_full_hash, e.path): e for e in pgroup}
                for fut in as_completed(full_futs):
                    entry = full_futs[fut]
                    try:
                        entry.full_hash = fut.result()
                    except Exception:
                        entry.full_hash = None
                    if progress_callback:
                        progress_callback({"type": "file", "stage": "full", "path": entry.path})

                # group by full hash -> exact duplicates
                full_groups = defaultdict(list)
                for e in pgroup:
                    full_groups[e.full_hash].append(e)
                for fh, flist in full_groups.items():
                    if fh and len(flist) > 1:
                        duplicates.append({"type": "exact", "hash": fh, "files": flist, "suggested": _suggest_best(flist)})

    # Step 2: perceptual image clustering (images not already in exact groups)
    if IMAGEHASH_AVAILABLE:
        if progress_callback:
            progress_callback({"type": "status", "text": "Running perceptual image pass (phash)..."})
        # build list of image entries not already part of an exact duplicate
        exact_members = set()
        for g in duplicates:
            for e in g["files"]:
                exact_members.add(e.path)

        image_entries = [e for e in files if e.path not in exact_members and e.phash is not None]

        # simple O(N^2) clustering using hamming distance threshold (works fine for typical folder sizes)
        used = [False] * len(image_entries)
        for i, e in enumerate(image_entries):
            if used[i]:
                continue
            cluster = [e]
            used[i] = True
            for j in range(i + 1, len(image_entries)):
                if used[j]:
                    continue
                other = image_entries[j]
                try:
                    dist = e.phash - other.phash
                except Exception:
                    # fallback: string hamming (less accurate)
                    try:
                        s1 = e.phash.__str__()
                        s2 = other.phash.__str__()
                        dist = sum(c1 != c2 for c1, c2 in zip(s1, s2))
                    except Exception:
                        dist = 999
                if dist <= PHASH_HAMMING_THRESHOLD:
                    cluster.append(other)
                    used[j] = True
            if len(cluster) > 1:
                duplicates.append({"type": "perceptual", "hash": None, "files": cluster, "suggested": _suggest_best(cluster)})

    if progress_callback:
        progress_callback({"type": "status", "text": "Scan complete"})

    return duplicates
