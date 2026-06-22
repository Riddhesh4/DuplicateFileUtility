"""Thumbnail utilities using Pillow.

Returns ImageTk.PhotoImage objects suitable for use in Tkinter.
"""
from __future__ import annotations

import io
import os
import subprocess
import logging

try:
    from PIL import Image, ImageTk, ImageDraw
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

try:
    import cv2
    CV2_AVAILABLE = True
except Exception:
    CV2_AVAILABLE = False


VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".mpg",
    ".mpeg",
}


def _get_debug_logger() -> logging.Logger:
    logger = logging.getLogger("duplicate_finder.debug")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    log_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "duplicate_finder_debug.log"))
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


LOGGER = _get_debug_logger()


def _is_low_information_frame(frame) -> bool:
    """Return True when frame is likely a black/fade frame or nearly blank."""
    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean_val, std_val = cv2.meanStdDev(gray)
        mean_brightness = float(mean_val[0][0])
        contrast = float(std_val[0][0])
        return mean_brightness < 12.0 and contrast < 10.0
    except Exception:
        return False


def _thumbnail_from_video_cv2(path: str, size: tuple):
    if not CV2_AVAILABLE or not PIL_AVAILABLE:
        LOGGER.debug("cv2 thumbnail unavailable path=%s cv2=%s pil=%s", path, CV2_AVAILABLE, PIL_AVAILABLE)
        return None
    cap = None
    try:
        LOGGER.debug("cv2 thumbnail start path=%s size=%s", path, size)
        cap = cv2.VideoCapture(path)
        if not cap or not cap.isOpened():
            LOGGER.debug("cv2 thumbnail open failed path=%s", path)
            return None

        # Pick a representative frame instead of blindly using the first frame,
        # which is often black for videos with fade-in intros.
        frame = None
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        probe_positions = [0.02, 0.08, 0.15, 0.25, 0.4, 0.6]

        for pos in probe_positions:
            if frame_count > 0:
                target = max(0, min(frame_count - 1, int(frame_count * pos)))
                cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            ok, candidate = cap.read()
            if not ok or candidate is None:
                continue
            if _is_low_information_frame(candidate):
                if frame is None:
                    frame = candidate
                continue
            frame = candidate
            break

        if frame is None:
            LOGGER.debug("cv2 thumbnail no frame path=%s", path)
            return None

        # Convert BGR (OpenCV default) to RGB for Pillow.
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        im = Image.fromarray(frame_rgb)
        im.thumbnail(size, Image.LANCZOS)
        LOGGER.debug("cv2 thumbnail success path=%s final_size=%s", path, im.size)
        return ImageTk.PhotoImage(im)
    except Exception as exc:
        LOGGER.exception("cv2 thumbnail error path=%s size=%s err=%s", path, size, exc)
        return None
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


def _thumbnail_from_video_ffmpeg(path: str, size: tuple):
    if not PIL_AVAILABLE:
        LOGGER.debug("ffmpeg thumbnail unavailable path=%s pil=%s", path, PIL_AVAILABLE)
        return None
    # Requires ffmpeg executable in PATH. Probe multiple timestamps to avoid black intros.
    for sec in (0.2, 0.8, 1.5, 3.0):
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(sec),
            "-i",
            path,
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, check=False)
            if proc.returncode != 0 or not proc.stdout:
                LOGGER.debug("ffmpeg thumbnail attempt failed path=%s sec=%s rc=%s", path, sec, proc.returncode)
                continue
            with Image.open(io.BytesIO(proc.stdout)) as im:
                im.thumbnail(size, Image.LANCZOS)
                LOGGER.debug("ffmpeg thumbnail success path=%s sec=%s final_size=%s", path, sec, im.size)
                return ImageTk.PhotoImage(im.copy())
        except Exception as exc:
            LOGGER.exception("ffmpeg thumbnail error path=%s sec=%s err=%s", path, sec, exc)
            continue
    return None


def make_thumbnail(path: str, size: tuple = (256, 256)):
    """Return a Tk-compatible thumbnail or None if not possible."""
    LOGGER.debug("make_thumbnail path=%s size=%s", path, size)
    if not PIL_AVAILABLE:
        LOGGER.debug("make_thumbnail aborted no PIL path=%s", path)
        return None

    ext = os.path.splitext(path)[1].lower()

    if ext in VIDEO_EXTS:
        # Prefer OpenCV for fast in-process frame extraction; fallback to ffmpeg binary.
        video_thumb = _thumbnail_from_video_cv2(path, size)
        if video_thumb is None:
            video_thumb = _thumbnail_from_video_ffmpeg(path, size)
        if video_thumb is not None:
            LOGGER.debug("make_thumbnail video success path=%s", path)
            return video_thumb

    try:
        with Image.open(path) as im:
            im.thumbnail(size, Image.LANCZOS)
            LOGGER.debug("make_thumbnail image success path=%s final_size=%s", path, im.size)
            return ImageTk.PhotoImage(im.copy())
    except Exception as exc:
        LOGGER.exception("make_thumbnail image fallback path=%s err=%s", path, exc)
        # Fallback for non-image files (e.g. mp4): render a simple placeholder icon.
        try:
            w, h = int(size[0]), int(size[1])
            img = Image.new("RGB", (max(24, w), max(24, h)), color=(230, 234, 240))
            draw = ImageDraw.Draw(img)
            draw.rectangle((2, 2, img.width - 3, img.height - 3), outline=(180, 188, 200), width=2)
            ext = path.rsplit(".", 1)[-1].upper() if "." in path else "FILE"
            label = ext[:6]
            text_w = int(len(label) * 7)
            draw.text(((img.width - text_w) // 2, (img.height // 2) - 7), label, fill=(72, 84, 104))
            LOGGER.debug("make_thumbnail placeholder success path=%s size=%s", path, (img.width, img.height))
            return ImageTk.PhotoImage(img)
        except Exception as fallback_exc:
            LOGGER.exception("make_thumbnail placeholder failed path=%s err=%s", path, fallback_exc)
            return None
