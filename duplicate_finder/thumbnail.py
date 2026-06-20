"""Thumbnail utilities using Pillow.

Returns ImageTk.PhotoImage objects suitable for use in Tkinter.
"""
from __future__ import annotations

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False


def make_thumbnail(path: str, size: tuple = (256, 256)):
    """Return a Tk-compatible thumbnail or None if not possible."""
    if not PIL_AVAILABLE:
        return None
    try:
        with Image.open(path) as im:
            im.thumbnail(size, Image.LANCZOS)
            return ImageTk.PhotoImage(im.copy())
    except Exception:
        return None
