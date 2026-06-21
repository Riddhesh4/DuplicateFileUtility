import os
import tempfile
import shutil
import pytest
from duplicate_finder import scanner


def _make_file(path, size=1024, content: bytes = None):
    data = content if content is not None else b"\0" * size
    with open(path, "wb") as fh:
        fh.write(data)


def test_find_exact_duplicates(tmp_path):
    d1 = tmp_path / "a"
    d2 = tmp_path / "b"
    d1.mkdir()
    d2.mkdir()
    f1 = d1 / "file1.bin"
    f2 = d2 / "file2.bin"
    _make_file(f1, size=1024, content=b"hello world")
    _make_file(f2, size=1024, content=b"hello world")

    groups = scanner.find_duplicates([str(d1), str(d2)], workers=2)
    # expect one exact group with two files
    assert any(g["type"] == "exact" and len(g["files"]) == 2 for g in groups)


def test_no_duplicates(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    _make_file(d / "f1", size=256, content=b"a")
    _make_file(d / "f2", size=512, content=b"b")
    groups = scanner.find_duplicates([str(d)], workers=2)
    assert groups == []


def test_perceptual_duplicates(tmp_path):
    try:
        from PIL import Image, ImageDraw
    except Exception:
        pytest.skip("Pillow not available")

    # create two visually similar images saved in different formats
    img1 = tmp_path / "img1.png"
    img2 = tmp_path / "img2.jpg"
    im = Image.new("RGB", (64, 64), color=(123, 100, 200))
    draw = ImageDraw.Draw(im)
    draw.rectangle([8, 8, 56, 56], outline=(255, 255, 255))
    im.save(img1)
    im.save(img2, quality=85)

    groups = scanner.find_duplicates([str(tmp_path)], workers=2)

    assert any(g["type"] == "perceptual" and len(g["files"]) >= 2 for g in groups)


def test_scan_cache_reuse(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    f1 = d / "file1.bin"
    f2 = d / "file2.bin"
    f1.write_bytes(b"hello world")
    f2.write_bytes(b"hello world")
    cache_db = tmp_path / "scanner_cache.db"

    # initial scan populates the cache file
    groups = scanner.find_duplicates([str(d)], workers=1, cache_path=str(cache_db))
    assert any(g["type"] == "exact" and len(g["files"]) == 2 for g in groups)
    assert cache_db.exists()

    # second scan should reuse cached hashes without recomputing them
    called = {"partial": 0, "full": 0}

    def fake_partial(path):
        called["partial"] += 1
        return scanner._compute_partial_hash(path)

    def fake_full(path):
        called["full"] += 1
        return scanner._compute_full_hash_worker(path)

    monkeypatch.setattr(scanner, "_compute_partial_hash", fake_partial)
    monkeypatch.setattr(scanner, "_compute_full_hash_worker", fake_full)

    groups = scanner.find_duplicates([str(d)], workers=1, cache_path=str(cache_db))
    assert any(g["type"] == "exact" and len(g["files"]) == 2 for g in groups)
    assert called["partial"] == 0
    assert called["full"] == 0
