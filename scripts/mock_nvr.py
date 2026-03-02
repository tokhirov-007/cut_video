import os
import shutil
import time
from datetime import datetime, timedelta
import imageio_ffmpeg

# This script creates a dummy folder structure with fake video files 
# to simulate a remote NVR server.
# Run this, then point REMOTE_HOST to 127.0.0.1 in .env

BASE_DIR = "mock_remote_videos"
DATE = "2026-02-24"
ROOMS = ["room_1", "room_2", "room_3"]

import subprocess

def create_mock_nvr():
    if os.path.exists(BASE_DIR):
        shutil.rmtree(BASE_DIR)
    
    for room in ROOMS:
        room_path = os.path.join(BASE_DIR, DATE, room)
        os.makedirs(room_path, exist_ok=True)
        
        if room == "room_1":
            files = [
                ("cam1_09-00.mp4", 120),
                ("cam1_13-10.mp4", 60),
                ("cam1_14-05.mp4", 120),
            ]
            
            for fname, duration in files:
                fpath = os.path.join(room_path, fname)
                print(f"Generating mock video: {fname}")
                ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                cmd = [
                    ffmpeg_exe, "-y", "-f", "lavfi", "-i", 
                    f"testsrc=duration={duration}:size=640x480:rate=10", 
                    "-pix_fmt", "yuv420p", fpath, "-loglevel", "quiet"
                ]
                subprocess.run(cmd, check=True)

if __name__ == "__main__":
    create_mock_nvr()
    print(f"Mock NVR created at {os.path.abspath(BASE_DIR)}")
