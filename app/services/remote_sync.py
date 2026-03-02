import os
import paramiko
from ..config import settings
import logging

logger = logging.getLogger(__name__)

class RemoteSyncService:
    def __init__(self):
        self.host = settings.REMOTE_HOST
        self.port = settings.REMOTE_PORT
        self.username = settings.REMOTE_USER
        self.password = settings.REMOTE_PASSWORD
        self.ssh = None

    def _connect(self):
        if self.host == "local":
            return None
        if self.ssh is None:
            self.ssh = paramiko.SSHClient()
            self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.ssh.connect(self.host, port=self.port, username=self.username, password=self.password)
        return self.ssh.open_sftp()

    def sync_room_videos(self, date_str: str, room: str):
        """
        Downloads all videos for a specific room and date.
        """
        local_dir = os.path.join(settings.DOWNLOAD_PATH, date_str, room)
        os.makedirs(local_dir, exist_ok=True)

        if self.host == "local":
            import shutil
            remote_dir = os.path.join(settings.REMOTE_BASE_PATH, date_str, room)
            if not os.path.exists(remote_dir):
                logger.warning(f"Local source directory {remote_dir} not found.")
                return []
            
            downloaded = []
            for filename in os.listdir(remote_dir):
                if not filename.endswith((".mp4", ".mkv", ".avi")):
                    continue
                src = os.path.join(remote_dir, filename)
                dst = os.path.join(local_dir, filename)
                if not os.path.exists(dst) or os.path.getsize(src) != os.path.getsize(dst):
                    shutil.copy2(src, dst)
                downloaded.append(dst)
            return downloaded

        sftp = self._connect()
        remote_dir = f"{settings.REMOTE_BASE_PATH}/{date_str}/{room}/"
        local_dir = os.path.join(settings.DOWNLOAD_PATH, date_str, room)
        os.makedirs(local_dir, exist_ok=True)

        logger.info(f"Syncing {remote_dir} to {local_dir}")
        
        try:
            files = sftp.listdir(remote_dir)
            downloaded = []
            for filename in files:
                if not filename.endswith((".mp4", ".mkv", ".avi")):
                    continue
                
                remote_file = remote_dir + filename
                local_file = os.path.join(local_dir, filename)
                
                # Simple check: size comparison or existence
                if os.path.exists(local_file):
                    remote_stat = sftp.stat(remote_file)
                    if remote_stat.st_size == os.path.getsize(local_file):
                        logger.debug(f"File {filename} already exists and matches size. Skipping.")
                        downloaded.append(local_file)
                        continue

                logger.info(f"Downloading {filename}...")
                sftp.get(remote_file, local_file)
                downloaded.append(local_file)
            
            return downloaded
        except FileNotFoundError:
            logger.warning(f"Remote directory {remote_dir} not found.")
            return []
        finally:
            sftp.close()

    def close(self):
        if self.ssh:
            self.ssh.close()
            self.ssh = None
