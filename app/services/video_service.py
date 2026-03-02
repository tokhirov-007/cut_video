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

        # Extract info from filename
        # New format: 504-2_v_bino_0022_0022_20260218135939_20260218150109_1589189.mp4
        # Old format: room1_12-00.mp4
        filename = os.path.basename(file_path)
        
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
            room = match_old.group(1)
            start_hour, start_min = map(int, match_old.groups()[1:])
            return {
                "duration": duration, 
                "start_time": (start_hour, start_min, 0),
                "room": room
            }
        
        return {"duration": duration, "start_time": None, "room": None}

    def cut_segment(self, input_file, start_sec, duration_sec, output_file):
        """Cuts a video segment. Accurate output seeking."""
        cmd = [
            self._get_ffmpeg_exe(), "-y",
            "-i", input_file,
            "-ss", str(start_sec),
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
