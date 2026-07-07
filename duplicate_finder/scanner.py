"""Core scanning utilities for Duplicate Finder.

Features:
- Walk up to 6 user-supplied paths and collect files
- Content-based duplicate detection using two-stage hashing
- Fast preliminary hash (xxhash, fallback CRC32), then deep SHA256
- Optional perceptual image hashing (imagehash) to detect visually
  identical images even when file bytes differ
"""
from __future__ import annotations

import os
import time
import hashlib
import logging
import sqlite3
import zlib
from collections import defaultdict
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
import mmap
from typing import List, Optional, Callable, Dict, Any, TypedDict, Literal, Union, Protocol, cast, TYPE_CHECKING

# Check optional dependencies (use functions to avoid Pylance "constant reassignment" error)
def _check_pil_available() -> bool:
    try:
        from PIL import Image
        return True
    except Exception:
        return False

def _check_imagehash_available() -> bool:
    try:
        import imagehash
        return True
    except Exception:
        return False

def _check_xxhash_available() -> bool:
    try:
        import xxhash
        return True
    except Exception:
        return False

PIL_AVAILABLE = _check_pil_available()
IMAGEHASH_AVAILABLE = _check_imagehash_available()
XXHASH_AVAILABLE = _check_xxhash_available()

# Perform actual imports for runtime use (imagehash and xxhash only; PIL.Image imported locally in functions)
try:
    import imagehash
except Exception:
    pass

try:
    import xxhash
except Exception:
    pass

if TYPE_CHECKING:
    # For Pylance: ensure xxhash types are visible during type checking
    try:
        import xxhash as xxhash_for_types
    except Exception:
        xxhash_for_types = None  # type: ignore[assignment]


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


class HashAlgorithm(Protocol):
    """Protocol for hash algorithm objects (hashlib, xxhash, etc.)."""

    def update(self, __data: bytes, /) -> None:
        """Update the hash with data (position-only parameter)."""
        ...

    def hexdigest(self) -> str:
        """Return the digest as a hex string."""
        ...


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
                fast_hash TEXT,
                partial_hash TEXT,
                full_hash TEXT,
                phash TEXT,
                width INTEGER,
                height INTEGER,
                updated_at REAL NOT NULL
            )
            """
        )
        self._ensure_columns()
        self.conn.commit()

    def _ensure_columns(self) -> None:
        # Keep cache schema compatible with older DB files created before fast_hash existed.
        cursor = self.conn.execute("PRAGMA table_info(file_hashes)")
        cols = {row[1] for row in cursor.fetchall()}
        if "fast_hash" not in cols:
            self.conn.execute("ALTER TABLE file_hashes ADD COLUMN fast_hash TEXT")

    def close(self) -> None:
        try:
            self.conn.commit()
        except Exception:
            pass
        self.conn.close()

    def get_records(self, paths: List[str]) -> Dict[str, CacheRecord]:
        if not paths:
            return {}

        records: Dict[str, CacheRecord] = {}
        batch_size = 500
        for start in range(0, len(paths), batch_size):
            batch = paths[start : start + batch_size]
            placeholders = ",".join("?" for _ in batch)
            query = "SELECT path,size,mtime,fast_hash,partial_hash,full_hash,phash,width,height FROM file_hashes WHERE path IN (" + placeholders + ")"
            cursor = self.conn.execute(query, batch)
            for row in cursor.fetchall():
                records[row[0]] = {
                    "size": row[1],
                    "mtime": row[2],
                    "fast_hash": row[3],
                    "partial_hash": row[4],
                    "full_hash": row[5],
                    "phash": row[6],
                    "width": row[7],
                    "height": row[8],
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
            INSERT INTO file_hashes (path, size, mtime, fast_hash, partial_hash, full_hash, phash, width, height, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                size=excluded.size,
                mtime=excluded.mtime,
                fast_hash=excluded.fast_hash,
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
                entry.fast_hash,
                entry.partial_hash,
                entry.full_hash,
                phash_value,
                entry.width,
                entry.height,
                time.time(),
            ),
        )


class DuplicateGroup(TypedDict):
    type: str
    hash: Optional[str]
    files: List["FileEntry"]
    suggested: int


class CacheRecord(TypedDict, total=False):
    size: int
    mtime: float
    fast_hash: Optional[str]
    partial_hash: Optional[str]
    full_hash: Optional[str]
    phash: Optional[str]
    width: Optional[int]
    height: Optional[int]


class ProgressStatus(TypedDict):
    type: Literal["status"]
    text: str


class ProgressTotal(TypedDict):
    type: Literal["total"]
    total: int


class ProgressFile(TypedDict):
    type: Literal["file"]
    stage: str
    path: str


ProgressMessage = Union[ProgressStatus, ProgressTotal, ProgressFile, Dict[str, Any]]
ProgressCallback = Callable[[ProgressMessage], None]


@dataclass
class FileEntry:
    path: str
    size: int
    mtime: float
    name: str
    ext: str
    width: Optional[int] = None
    height: Optional[int] = None
    fast_hash: Optional[str] = None
    partial_hash: Optional[str] = None
    full_hash: Optional[str] = None
    phash: Optional[object] = None  # imagehash.ImageHash when available


def _is_image_file(path: str) -> bool:
    if not PIL_AVAILABLE:
        return False
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def _apply_cache_to_entry(entry: FileEntry, record: CacheRecord) -> None:
    if record.get("size") != entry.size or record.get("mtime") != entry.mtime:
        return
    entry.fast_hash = record.get("fast_hash")
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
    """Compute a fast fingerprint for a file, preferring xxhash with CRC32 fallback."""
    try:
        if XXHASH_AVAILABLE:
            assert XXHASH_AVAILABLE, "xxhash must be available"  # type guard
            h = cast(HashAlgorithm, xxhash.xxh64())
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
                    h.update(chunk)
            return h.hexdigest()
        else:
            crc = 0
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
                    crc = zlib.crc32(chunk, crc)
            return f"{crc & 0xFFFFFFFF:08x}"
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


def _compare_candidate_group(group: List[FileEntry], progress_callback: Optional[ProgressCallback] = None) -> tuple[List[DuplicateGroup], List[FileEntry]]:
    duplicates: List[DuplicateGroup] = []
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
        from PIL import Image
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


def find_duplicates(paths: List[str], workers: Optional[int] = 4, progress_callback: Optional[ProgressCallback] = None, use_fast_hash: bool = False, verify_fast_hash: bool = False, use_direct_compare: bool = False, cache_path: Optional[str] = None, files: Optional[List[FileEntry]] = None) -> List[DuplicateGroup]:
    """Scan given paths and return list of duplicate groups.

    Each group is a dict: {'type': 'exact'|'perceptual', 'hash': str|None, 'files': List[FileEntry], 'suggested': int}
    """
    if files is None:
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

        duplicates: List[DuplicateGroup] = []

        # Step 1: content hash pass over all files (name/size independent).
        if progress_callback:
            progress_callback({"type": "status", "text": "Computing fast content hashes..."})

        fast_pending = [e for e in files if not e.fast_hash]
        if progress_callback:
            progress_callback({"type": "total", "stage": "fast", "total": len(fast_pending), "text": "Fast hash pass"})

        if fast_pending:
            with ThreadPoolExecutor(max_workers=max(1, workers or 1)) as ex:
                fast_futs = {ex.submit(_compute_fast_hash_worker, e.path): e for e in fast_pending}
                for fut in as_completed(fast_futs):
                    entry = fast_futs[fut]
                    try:
                        entry.fast_hash = fut.result()
                    except Exception:
                        entry.fast_hash = None
                    if progress_callback:
                        progress_callback({"type": "file", "stage": "fast", "path": entry.path})

        fast_map: Dict[str, List[FileEntry]] = defaultdict(list)
        for entry in files:
            if entry.fast_hash:
                fast_map[entry.fast_hash].append(entry)

        full_candidates = [e for group in fast_map.values() if len(group) > 1 for e in group]
        if progress_callback:
            progress_callback({"type": "status", "text": "Verifying duplicate candidates with SHA-256..."})

        full_pending = [e for e in full_candidates if not e.full_hash]
        if progress_callback:
            progress_callback({"type": "total", "stage": "full", "total": len(full_pending), "text": "Deep hash pass"})

        if full_pending:
            with ThreadPoolExecutor(max_workers=max(1, workers or 1)) as ex:
                full_futs = {ex.submit(_compute_full_hash_worker, e.path): e for e in full_pending}
                for fut in as_completed(full_futs):
                    entry = full_futs[fut]
                    try:
                        entry.full_hash = fut.result()
                    except Exception:
                        entry.full_hash = None
                    if progress_callback:
                        progress_callback({"type": "file", "stage": "full", "path": entry.path})

        full_map: Dict[str, List[FileEntry]] = defaultdict(list)
        for entry in full_candidates:
            if entry.full_hash:
                full_map[entry.full_hash].append(entry)

        for full_hash, group in full_map.items():
            if len(group) > 1:
                duplicates.append({"type": "exact", "hash": full_hash, "files": group, "suggested": _suggest_best(group)})

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
