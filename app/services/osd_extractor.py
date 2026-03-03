"""
OSD Timestamp Extractor
-----------------------
Extracts camera date/time from On-Screen Display (OSD) text burned into
video corners by the camera itself. Works independent of filename format.

Strategy:
  1. Extract 3-5 frames from the video (beginning, small offset, middle)
  2. Crop each corner (top-left, top-right, bottom-left, bottom-right)
  3. Preprocess: grayscale, threshold, sharpen to improve OCR
  4. Run pytesseract OCR on each crop
  5. Parse any recognized date/time pattern
  6. Return (datetime, room_text_if_any)
"""
import subprocess
import os
import re
import logging
import tempfile
from datetime import datetime

logger = logging.getLogger(__name__)

# Supports many camera OSD layouts
DATETIME_PATTERNS = [
    # MM-DD-YYYY HH:MM:SS (common US camera OSD format)
    (r'(\d{2})[-./](\d{2})[-./](\d{4})\s+(?:\w+\s+)?(\d{2}):(\d{2}):?(\d{2})?',
     lambda m: datetime(int(m[2]), int(m[0]), int(m[1]), int(m[3]), int(m[4]), int(m[5] or 0))),
    # 2026-02-23 12:00:05
    (r'(\d{4})[-./](\d{2})[-./](\d{2})\s+(?:\w+\s+)?(\d{2}):(\d{2}):?(\d{2})?', 
     lambda m: datetime(int(m[0]), int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5] or 0))),
    # 23/02/2026 12:00:05
    (r'(\d{2})[-./](\d{2})[-./](\d{4})\s+(\d{2}):(\d{2}):?(\d{2})?',
     lambda m: datetime(int(m[2]), int(m[1]), int(m[0]), int(m[3]), int(m[4]), int(m[5] or 0))),
]


def _extract_frame(video_path: str, offset_sec: float, output_path: str, ffmpeg_exe: str) -> bool:
    """Extract a single frame from the video at the given second offset."""
    try:
        cmd = [
            ffmpeg_exe, "-y",
            "-ss", str(offset_sec),
            "-i", video_path,
            "-vframes", "1",
            "-q:v", "2",
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        return result.returncode == 0 and os.path.exists(output_path)
    except Exception as e:
        logger.debug(f"Frame extraction failed at {offset_sec}s: {e}")
        return False


def _preprocess_for_ocr(img_array):
    """Apply grayscale + thresholding to improve OCR accuracy."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(img_array, cv2.COLOR_BGR2GRAY)
    # Adaptive thresholding works well for camera OSD text
    thresh = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 11, 2
    )
    # Also try with simple threshold for white-on-dark OSD
    _, simple_thresh = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)
    return [thresh, simple_thresh, gray]


def _ocr_image(img_array) -> str:
    """Run OCR on image and return raw text."""
    try:
        import pytesseract
        from PIL import Image
        import numpy as np
        # Convert to PIL
        pil_img = Image.fromarray(img_array)
        # Use psm 6 (assume a single uniform block of text)
        config = "--psm 6"
        text = pytesseract.image_to_string(pil_img, config=config)
        return text.strip()
    except Exception as e:
        logger.debug(f"OCR failed: {e}")
        return ""


def _parse_datetime_from_text(text: str) -> datetime | None:
    """Try all known patterns to parse a datetime from OCR text."""
    text = text.replace("\n", " ").replace("\r", " ")
    for pattern, builder in DATETIME_PATTERNS:
        m = re.search(pattern, text)
        if m:
            try:
                groups = list(m.groups())
                # Fill missing optional seconds
                while len(groups) < 6:
                    groups.append("0")
                dt = builder(groups)
                # Sanity check: year must be between 2000 and 2100
                if 2000 <= dt.year <= 2100:
                    return dt
            except Exception:
                continue
    return None


def _crop_corners(img_array, corner_ratio: float = 0.25):
    """Return 4 corner crops of the image."""
    import numpy as np
    h, w = img_array.shape[:2]
    cw = int(w * corner_ratio)
    ch = int(h * corner_ratio)
    return {
        "top_left":     img_array[:ch,    :cw],
        "top_right":    img_array[:ch,    w - cw:],
        "bottom_left":  img_array[h - ch:, :cw],
        "bottom_right": img_array[h - ch:, w - cw:],
        "full":         img_array,                  # fallback
    }


def extract_osd_datetime(video_path: str, ffmpeg_exe: str = "ffmpeg", 
                          duration_sec: float = None) -> datetime | None:
    """
    Main entry point. Tries multiple frames and corners to find camera OSD datetime.
    Returns a datetime object or None if nothing could be found.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        logger.warning("opencv-python not installed – OSD extraction skipped")
        return None

    # Choose frame offsets: start, +2s, +5s, and if known, middle
    offsets = [0.5, 2.0, 5.0]
    if duration_sec and duration_sec > 20:
        offsets.append(duration_sec / 2)

    with tempfile.TemporaryDirectory() as tmpdir:
        for offset in offsets:
            frame_path = os.path.join(tmpdir, f"frame_{int(offset)}.jpg")
            if not _extract_frame(video_path, offset, frame_path, ffmpeg_exe):
                continue

            img = cv2.imread(frame_path)
            if img is None:
                continue

            corners = _crop_corners(img)
            for corner_name, crop in corners.items():
                if crop.size == 0:
                    continue
                for preprocessed in _preprocess_for_ocr(crop):
                    text = _ocr_image(preprocessed)
                    if text:
                        dt = _parse_datetime_from_text(text)
                        if dt:
                            logger.info(
                                f"OSD timestamp found in {corner_name} "
                                f"at offset {offset}s: {dt} (text: {text!r})"
                            )
                            return dt

    logger.debug(f"No OSD timestamp found in {video_path}")
    return None
