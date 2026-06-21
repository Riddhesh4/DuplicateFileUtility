"""Core scanning utilities for Duplicate Finder.

Features:
- Walk up to 6 user-supplied paths and collect files
- Fast filtering by file size, then partial-hash, then full SHA256
- Optional perceptual image hashing (imagehash) to detect visually
  identical images even when file bytes differ
"""
from __future__ import annotations

import os
import time
import hashlib
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
import mmap
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
try:
    import xxhash
    XXHASH_AVAILABLE = True
except Exception:
    XXHASH_AVAILABLE = False

CHUNK_SIZE = 8 * 1024 * 1024
PARTIAL_READ = 256 * 1024
# Use mmap for large files to reduce Python-level read overhead when beneficial
MMAP_MIN_SIZE = 2 * 1024 * 1024
MMAP_MAX_SIZE = 512 * 1024 * 1024

# common image file extensions — used to avoid opening non-image files with PIL
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'}

# Hamming distance threshold for perceptual image hashing (phash)
# Increase if you expect more lossy transformations to still be considered duplicates
PHASH_HAMMING_THRESHOLD = 10

logger = logging.getLogger("duplicate_finder.scanner")


class HashCache:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_hashes (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                partial_hash TEXT,
                full_hash TEXT,
                phash TEXT,
                width INTEGER,
                height INTEGER,
                updated_at REAL NOT NULL
            )
            """
        )
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
        except Exception:
            pass
        self.conn.close()

    def get_records(self, paths: List[str]) -> Dict[str, dict]:
        if not paths:
            return {}

        records: Dict[str, dict] = {}
        batch_size = 500
        for start in range(0, len(paths), batch_size):
            batch = paths[start : start + batch_size]
            placeholders = ",".join("?" for _ in batch)
            cursor = self.conn.execute(
                f"SELECT path,size,mtime,partial_hash,full_hash,phash,width,height FROM file_hashes WHERE path IN ({placeholders})",
                batch,
            )
            for row in cursor.fetchall():
                records[row[0]] = {
                    "size": row[1],
                    "mtime": row[2],
                    "partial_hash": row[3],
                    "full_hash": row[4],
                    "phash": row[5],
                    "width": row[6],
                    "height": row[7],
                }
        return records

    def update_entry(self, entry: "FileEntry") -> None:
        phash_value = None
        if entry.phash is not None:
            try:
                phash_value = str(entry.phash)
            except Exception:
                phash_value = None
        self.conn.execute(
            """
            INSERT INTO file_hashes (path, size, mtime, partial_hash, full_hash, phash, width, height, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size=excluded.size,
                mtime=excluded.mtime,
                partial_hash=excluded.partial_hash,
                full_hash=excluded.full_hash,
                phash=excluded.phash,
                width=excluded.width,
                height=excluded.height,
                updated_at=excluded.updated_at
            """,
            (
                entry.path,
                entry.size,
                entry.mtime,
                entry.partial_hash,
                entry.full_hash,
                phash_value,
                entry.width,
                entry.height,
                time.time(),
            ),
        )


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


def _apply_cache_to_entry(entry: FileEntry, record: dict) -> None:
    if record["size"] != entry.size or record["mtime"] != entry.mtime:
        return
    entry.partial_hash = record.get("partial_hash")
    entry.full_hash = record.get("full_hash")
    entry.width = record.get("width")
    entry.height = record.get("height")
    phash = record.get("phash")
    if IMAGEHASH_AVAILABLE and phash:
        try:
            entry.phash = imagehash.hex_to_hash(phash)
        except Exception:
            entry.phash = None


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


def _compute_full_hash_worker(path: str) -> Optional[str]:
    """Process-friendly full-file hasher that uses mmap for mid/large files when helpful.

    This function is intentionally top-level so it can be pickled for ProcessPoolExecutor.
    It returns the hex digest or None on error.
    """
    try:
        size = os.path.getsize(path)
        h = hashlib.sha256()
        # Use mmap for medium-to-large files to reduce Python-level read overhead
        if MMAP_MIN_SIZE <= size <= MMAP_MAX_SIZE:
            with open(path, "rb") as fh:
                mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
                try:
                    h.update(mm)
                finally:
                    mm.close()
        else:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
                    h.update(chunk)
        return h.hexdigest()
    except Exception:
        # Avoid complex logging from worker processes; return None on failure
        return None


def _compute_fast_hash_worker(path: str) -> Optional[str]:
    """Compute a fast fingerprint for a file, preferring xxhash when available."""
    try:
        if XXHASH_AVAILABLE:
            h = xxhash.xxh64()
        else:
            h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _compare_files(path1: str, path2: str) -> bool:
    """Compare two files byte-for-byte, using mmap for medium-sized files."""
    try:
        size1 = os.path.getsize(path1)
        size2 = os.path.getsize(path2)
        if size1 != size2:
            return False
        if size1 == 0:
            return True

        if MMAP_MIN_SIZE <= size1 <= MMAP_MAX_SIZE:
            with open(path1, "rb") as f1, open(path2, "rb") as f2:
                mm1 = mmap.mmap(f1.fileno(), 0, access=mmap.ACCESS_READ)
                mm2 = mmap.mmap(f2.fileno(), 0, access=mmap.ACCESS_READ)
                try:
                    pos = 0
                    while pos < size1:
                        end = min(pos + CHUNK_SIZE, size1)
                        if mm1[pos:end] != mm2[pos:end]:
                            return False
                        pos = end
                    return True
                finally:
                    mm1.close()
                    mm2.close()

        with open(path1, "rb") as f1, open(path2, "rb") as f2:
            while True:
                b1 = f1.read(CHUNK_SIZE)
                b2 = f2.read(CHUNK_SIZE)
                if not b1 and not b2:
                    return True
                if b1 != b2:
                    return False
    except Exception:
        return False


def _compare_candidate_group(group: List[FileEntry], progress_callback: Optional[Callable[[Any], None]] = None) -> tuple[List[dict], List[FileEntry]]:
    duplicates: List[dict] = []
    remainder: List[FileEntry] = []
    pending = list(group)

    while pending:
        ref = pending.pop(0)
        cluster = [ref]
        next_pending: List[FileEntry] = []

        for other in pending:
            if _compare_files(ref.path, other.path):
                cluster.append(other)
            else:
                next_pending.append(other)
            if progress_callback:
                progress_callback({"type": "status", "text": f"Comparing {os.path.basename(other.path)}"})

        if len(cluster) > 1:
            duplicates.append({"type": "exact", "hash": None, "files": cluster, "suggested": _suggest_best(cluster)})
        else:
            remainder.append(ref)

        pending = next_pending

    return duplicates, remainder


def _compute_image_info(entry: FileEntry) -> None:
    if not PIL_AVAILABLE:
        return
    if entry.width is not None and entry.height is not None and entry.phash is not None:
        return
    # fast extension check to skip expensive Image.open() for non-image files
    if entry.ext not in IMAGE_EXTS:
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


def find_duplicates(paths: List[str], workers: Optional[int] = 4, progress_callback: Optional[Callable[[Any], None]] = None, use_fast_hash: bool = False, verify_fast_hash: bool = False, use_direct_compare: bool = False, cache_path: Optional[str] = None) -> List[dict]:
    """Scan given paths and return list of duplicate groups.

    Each group is a dict: {'type': 'exact'|'perceptual', 'hash': str|None, 'files': List[FileEntry], 'suggested': int}
    """
    files = _collect_files(paths)
    cache = HashCache(cache_path) if cache_path else None
    try:
        if cache:
            cached = cache.get_records([f.path for f in files])
            for entry in files:
                record = cached.get(entry.path)
                if record:
                    _apply_cache_to_entry(entry, record)

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
                # only attempt to open files with typical image extensions
                futures = [ex.submit(_compute_image_info, f) for f in files if f.ext in IMAGE_EXTS]
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
            # Flatten candidate files (size groups with >1 file)
            candidate_files = [e for g in candidate_groups for e in g]

            # Partial hashes (fast) for candidate files missing cached values
            partial_futs = {ex.submit(_compute_partial_hash, e.path): e for e in candidate_files if not e.partial_hash}
            for fut in as_completed(partial_futs):
                entry = partial_futs[fut]
                try:
                    entry.partial_hash = fut.result()
                except Exception:
                    entry.partial_hash = None
                if progress_callback:
                    progress_callback({"type": "file", "stage": "partial", "path": entry.path})

            for entry in candidate_files:
                if entry.partial_hash is None:
                    entry.partial_hash = _compute_partial_hash(entry.path)

            # Group by (size, partial_hash) to limit next stages
            partial_map = defaultdict(list)
            for e in candidate_files:
                partial_map[(e.size, e.partial_hash)].append(e)

            candidate_groups = [g for g in partial_map.values() if len(g) > 1]
            candidate_files = [e for g in candidate_groups for e in g]

            if use_direct_compare:
                direct_candidates: List[FileEntry] = []
                for group in candidate_groups:
                    if len(group) == 2:
                        left, right = group
                        if _compare_files(left.path, right.path):
                            duplicates.append({"type": "exact", "hash": None, "files": [left, right], "suggested": _suggest_best(group)})
                        else:
                            direct_candidates.extend(group)
                        continue

                    exact_groups, remainder = _compare_candidate_group(group, progress_callback=progress_callback)
                    duplicates.extend(exact_groups)
                    direct_candidates.extend(remainder)

                candidate_files = direct_candidates

            if use_fast_hash and candidate_files:
                # Compute a fast file fingerprint for candidate files.
                if progress_callback:
                    progress_callback({"type": "status", "text": "Computing fast file fingerprints..."})
                proc_workers = max(1, min(len(candidate_files), workers or 1))
                with ProcessPoolExecutor(max_workers=proc_workers) as pex:
                    fast_futs = {pex.submit(_compute_fast_hash_worker, e.path): e for e in candidate_files}
                    for fut in as_completed(fast_futs):
                        entry = fast_futs[fut]
                        try:
                            entry.fast_hash = fut.result()
                        except Exception:
                            entry.fast_hash = None
                        if progress_callback:
                            progress_callback({"type": "file", "stage": "fast", "path": entry.path})

                fast_map = defaultdict(list)
                for e in candidate_files:
                    fast_map[(e.size, e.partial_hash, e.fast_hash)].append(e)

                if verify_fast_hash:
                    full_candidates = []
                    for fkey, fgroup in fast_map.items():
                        if len(fgroup) < 2:
                            continue
                        full_candidates.extend(fgroup)
                    if full_candidates:
                        proc_workers = max(1, min(len(full_candidates), workers or 1))
                        with ProcessPoolExecutor(max_workers=proc_workers) as pex:
                            full_futs = {pex.submit(_compute_full_hash_worker, e.path): e for e in full_candidates}
                            for fut in as_completed(full_futs):
                                entry = full_futs[fut]
                                try:
                                    entry.full_hash = fut.result()
                                except Exception:
                                    entry.full_hash = None
                                if progress_callback:
                                    progress_callback({"type": "file", "stage": "full", "path": entry.path})

                        full_groups = defaultdict(list)
                        for e in full_candidates:
                            full_groups[e.full_hash].append(e)
                        for fh, flist in full_groups.items():
                            if fh and len(flist) > 1:
                                duplicates.append({"type": "exact", "hash": fh, "files": flist, "suggested": _suggest_best(flist)})
                    return duplicates
                else:
                    for fh_key, flist in fast_map.items():
                        if fh_key[2] and len(flist) > 1:
                            duplicates.append({"type": "exact", "hash": fh_key[2], "files": flist, "suggested": _suggest_best(flist)})
                    return duplicates

            if candidate_files:
                # limit processes to available workers but at least 1
                entries_to_hash = [e for e in candidate_files if not e.full_hash]
                if entries_to_hash:
                    proc_workers = max(1, min(len(entries_to_hash), max(1, min(workers or 1, 2))))
                    with ProcessPoolExecutor(max_workers=proc_workers) as pex:
                        full_futs = {pex.submit(_compute_full_hash_worker, e.path): e for e in entries_to_hash}
                        for fut in as_completed(full_futs):
                            entry = full_futs[fut]
                            try:
                                entry.full_hash = fut.result()
                            except Exception:
                                entry.full_hash = None
                            if progress_callback:
                                progress_callback({"type": "file", "stage": "full", "path": entry.path})

            # Group by full hash -> exact duplicates
            full_groups = defaultdict(list)
            for e in candidate_files:
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
    finally:
        if cache:
            for entry in files:
                cache.update_entry(entry)
            cache.conn.commit()
            cache.close()
