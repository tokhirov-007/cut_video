import subprocess
import os
import logging
import json
import re
from datetime import datetime, timedelta
import imageio_ffmpeg
from ..config import settings

logger = logging.getLogger(__name__)

class VideoService:
    @staticmethod
    def _get_ffmpeg_exe():
        if settings.FFMPEG_PATH and settings.FFMPEG_PATH != "ffmpeg":
            return settings.FFMPEG_PATH
        return imageio_ffmpeg.get_ffmpeg_exe()

    @staticmethod
    def _get_ffprobe_exe():
        if settings.FFPROBE_PATH and settings.FFPROBE_PATH != "ffprobe":
            return settings.FFPROBE_PATH
        # imageio-ffmpeg usually only provides ffmpeg. 
        # But we can try to guess ffprobe location or just use ffprobe if in path.
        return settings.FFPROBE_PATH

    @staticmethod
    def get_video_info(file_path):
        """Returns duration and start time from file name/metadata."""
        # 1. Try FFprobe first
        try:
            cmd = [
                VideoService._get_ffprobe_exe(), "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", file_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                duration = float(data['format']['duration'])
            else:
                raise Exception("ffprobe failed")
        except Exception:
            # 2. Fallback to parsing FFmpeg stderr
            logger.debug("ffprobe failed or missing, falling back to ffmpeg for metadata")
            cmd = [VideoService._get_ffmpeg_exe(), "-i", file_path]
            # ffmpeg outputs info to stderr when no output file is specified
            result = subprocess.run(cmd, capture_output=True, text=True)
            match = re.search(r"Duration:\s+(\d+):(\d+):(\d+\.\d+)", result.stderr)
            if match:
                h, m, s = map(float, match.groups())
                duration = h * 3600 + m * 60 + s
            else:
                duration = 0.0

        # Extract filename once – used by both OSD and fallback methods
        filename = os.path.basename(file_path)

        # ─────────────────────────────────────────────────────────────
        # PRIMARY METHOD: OCR – read date/time from camera OSD overlay
        # ─────────────────────────────────────────────────────────────
        try:
            from .osd_extractor import extract_osd_datetime
            osd_dt = extract_osd_datetime(
                file_path, 
                ffmpeg_exe=VideoService._get_ffmpeg_exe(),
                duration_sec=duration
            )
            if osd_dt:
                # Extract room from filename as best-effort (leading digits)
                fname_room = re.match(r'^(\d+)', filename)
                room = fname_room.group(1).strip() if fname_room else None
                logger.info(f"OCR OSD start_time={osd_dt.time()} for {filename}")
                return {
                    "duration": duration,
                    "start_time": (osd_dt.hour, osd_dt.minute, osd_dt.second),
                    "room": room
                }
        except Exception as osd_e:
            logger.debug(f"OSD extraction failed for {filename}: {osd_e}")

        # ─────────────────────────────────────────────────────────────
        # FALLBACK 1: Extract info from filename
        # New format: 504-2_v_bino_0022_0022_20260218135939_20260218150109_1589189.mp4
        # Old format: room1_12-00.mp4
        # ─────────────────────────────────────────────────────────────
        
        # Try new format first (stops at - or _)
        new_pattern = r'^([^-/_]+).*?(\d{14})_(\d{14})'
        match_new = re.search(new_pattern, filename)
        if match_new:
            room = match_new.group(1)
            start_ts = match_new.group(2)
            try:
                start_dt = datetime.strptime(start_ts, "%Y%m%d%H%M%S")
                return {
                    "duration": duration, 
                    "start_time": (start_dt.hour, start_dt.minute, start_dt.second),
                    "room": room
                }
            except ValueError:
                pass

        # Old format fallback (stops at - or _)
        old_pattern = r'^([^-/_]+)_(\d{2})-(\d{2})'
        match_old = re.search(old_pattern, filename)
        if match_old:
            room = match_old.group(1).strip()
            start_hour, start_min = map(int, match_old.groups()[1:])
            return {
                "duration": duration, 
                "start_time": (start_hour, start_min, 0),
                "room": room
            }

        # Simplest fallback: extract leading digits/word as room (e.g. "110 xona.mp4" -> "110")
        simple_pattern = re.match(r'^(\d+)', filename)
        if simple_pattern:
            room = simple_pattern.group(1).strip()
        else:
            fname_room_match = re.match(r'^([^\s_-]+)', filename)
            room = fname_room_match.group(1).strip() if fname_room_match else None
        
        # 3rd fallback: read creation_time from ffprobe container/stream tags
        # Handles cameras that embed recording datetime inside the video metadata
        try:
            cmd = [
                VideoService._get_ffprobe_exe(), "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", file_path
            ]
            result2 = subprocess.run(cmd, capture_output=True, text=True)
            if result2.returncode == 0:
                data2 = json.loads(result2.stdout)
                creation_time = None
                fmt_tags = data2.get("format", {}).get("tags", {})
                creation_time = fmt_tags.get("creation_time") or fmt_tags.get("date")
                if not creation_time:
                    for stream in data2.get("streams", []):
                        creation_time = stream.get("tags", {}).get("creation_time")
                        if creation_time:
                            break
                if creation_time:
                    creation_time = creation_time.replace("Z", "+00:00")
                    ct_dt = datetime.fromisoformat(creation_time)
                    import time as _time
                    utc_offset_sec = -_time.timezone if _time.daylight == 0 else -_time.altzone
                    ct_local = ct_dt.replace(tzinfo=None) + timedelta(seconds=utc_offset_sec)
                    fname_room_match = re.match(r'^([^-/_]+)', filename)
                    room = fname_room_match.group(1) if fname_room_match else None
                    logger.info(f"Extracted start_time from ffprobe tags: {ct_local} for {filename}")
                    return {
                        "duration": duration,
                        "start_time": (ct_local.hour, ct_local.minute, ct_local.second),
                        "room": room
                    }
        except Exception as tag_ex:
            logger.debug(f"ffprobe tag extraction failed for {filename}: {tag_ex}")

        return {"duration": duration, "start_time": None, "room": None}

    def cut_segment(self, input_file, start_sec, duration_sec, output_file):
        """Cuts a video segment using fast input-seek for accurate stream copy."""
        cmd = [
            self._get_ffmpeg_exe(), "-y",
            "-ss", str(start_sec),      # Input seek (before -i) = fast & accurate
            "-i", input_file,
            "-t", str(duration_sec),
            "-c:v", "copy",
            "-c:a", "aac",
            "-map", "0",
            "-avoid_negative_ts", "1",
            output_file
        ]
        subprocess.run(cmd, check=True, capture_output=True)

    def merge_segments(self, segment_list, output_file):
        """Merges multiple segments using concat demuxer."""
        if not segment_list:
            return
        
        list_file = output_file + ".txt"
        with open(list_file, "w") as f:
            for seg in segment_list:
                f.write(f"file '{os.path.abspath(seg)}'\n")
        
        cmd = [
            self._get_ffmpeg_exe(), "-y", "-f", "concat", "-safe", "0",
            "-i", list_file, "-c:v", "copy", "-c:a", "aac", output_file
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        os.remove(list_file)
