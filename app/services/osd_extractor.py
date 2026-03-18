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
  5. Parse any recognized date/time and room patterns
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
# Format: (pattern, lambda m, target_dt: datetime)
DATETIME_PATTERNS = [
    # YYYY-MM-DD (ISO-like)
    (r'(\d{4})[-./](\d{2})[-./](\d{2})\s+(?:\w+\s+)?(\d{2}):(\d{2}):?(\d{2})?', 
     lambda m, t: datetime(int(m[0]), int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5] or 0))),
    
    # DD-MM-YYYY or MM-DD-YYYY or DD-MM-YY HH:MM:SS
    (r'(\d{2})[-./](\d{2})[-./](\d{2,4})\s+(?:\w+\s+)?(\d{2}):(\d{2}):?(\d{2})?',
     None), # Handled by custom logic below
]

ROOM_PATTERNS = [
    r'\b(\d+)\s*-?\s*maruza\s*zali\b',
    r'\b(\d+)\s*-?\s*ma\'ruza\s*zali\b',
    r'\bmaruza\s*zali\s*-?\s*(\d+)\b',
    r'\bma\'ruza\s*zali\s*-?\s*(\d+)\b',
    r'(\d+)\s*-?\s*zali\b',
    r'(?:Xona|Room|XONA|Xo\'na|ROOM|Кабинет|Zal|Zali)[:\s]*([a-zA-Z-]*\d+[a-zA-Z-]*)',
    # Одиночные 3-4 цифры, НО только если они не окружены дефисами, точками или двоеточиями (т.е. это не часть даты/времени типа 2026-03), исключая годы 2024-2029
    r'(?<![-./:])\b(?!(?:202[4-9])\b)(\d{3,4})\b(?![-./:])',
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
    
    # Увеличиваем кадр в 2 раза для лучшего распознавания мелкого шрифта (например "8" вместо "AY")
    img_array = cv2.resize(img_array, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    
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


def _parse_datetime_from_text(text: str, target_date: datetime = None) -> datetime | None:
    """Try all known patterns to parse a datetime from OCR text."""
    text = text.replace("\n", " ").replace("\r", " ")
    for pattern, builder in DATETIME_PATTERNS:
        m = re.search(pattern, text)
        if not m:
            continue
            
        try:
            groups = list(m.groups())
            # Fill missing optional seconds
            while len(groups) < 6:
                groups.append("0")
            
            if builder:
                dt = builder(groups, target_date)
            else:
                # Custom logic for DD-MM vs MM-DD disambiguation
                d1, d2, year_raw = int(groups[0]), int(groups[1]), groups[2]
                year = int(year_raw)
                if len(year_raw) == 2:
                    year += 2000 # Assume 20xx
                
                h, m_val, s = int(groups[3]), int(groups[4]), int(groups[5] or 0)
                
                # Option A: d1=Month, d2=Day
                dt_a = None
                try:
                    if 1 <= d1 <= 12 and 1 <= d2 <= 31:
                        dt_a = datetime(year, d1, d2, h, m_val, s)
                except ValueError: pass
                
                # Option B: d1=Day, d2=Month
                dt_b = None
                try:
                    if 1 <= d2 <= 12 and 1 <= d1 <= 31:
                        dt_b = datetime(year, d2, d1, h, m_val, s)
                except ValueError: pass
                
                if dt_a and not dt_b: dt = dt_a
                elif dt_b and not dt_a: dt = dt_b
                elif dt_a and dt_b and target_date:
                    # Both valid? Pick the one that matches target_date
                    if dt_a.date() == target_date.date(): dt = dt_a
                    elif dt_b.date() == target_date.date(): dt = dt_b
                    else: dt = dt_b # Default to DD-MM (common in non-US)
                else:
                    dt = dt_b or dt_a
            
            # Sanity check: year must be between 2000 and 2100
            if dt and 2000 <= dt.year <= 2100:
                return dt
        except Exception:
            continue
    return None

def _parse_room_from_text(text: str) -> str | None:
    """Try to find a room number or name in OCR text."""
    # Clean up common OCR noise
    text = text.replace("\n", " ").replace("\r", " ")
    for pattern in ROOM_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            room = m.group(1) if m.groups() else m.group(0)
            return room.strip()
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


def extract_osd_info(video_path: str, ffmpeg_exe: str = "ffmpeg", 
                     duration_sec: float = None,
                     target_date: datetime = None) -> tuple[datetime | None, str | None]:
    """
    Main entry point. Tries multiple frames and corners to find camera OSD info.
    Returns (datetime, room) or (None, None).
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        logger.warning("opencv-python not installed – OSD extraction skipped")
        return None, None

    # Choose frame offsets: start, 2s, 5s
    offsets = [0.5, 2.0, 5.0]
    if duration_sec and duration_sec > 20:
        offsets.append(duration_sec / 2)

    found_dt = None
    found_room = None

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
                    if not text:
                        continue
                        
                    logger.info(f"RAW OCR TEXT ({corner_name}): {text!r}")
                    
                    if not found_dt and corner_name in ["top_left", "full"]:
                        found_dt = _parse_datetime_from_text(text, target_date=target_date)
                    if not found_room and corner_name in ["bottom_right", "full"]:
                        found_room = _parse_room_from_text(text)
                    
                    if found_dt and found_room:
                        logger.info(f"OSD complete: {found_dt}, room={found_room} (corner: {corner_name})")
                        return found_dt, found_room

    return found_dt, found_room
