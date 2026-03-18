import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Remote Server Configuration
    REMOTE_HOST: str = "example.com"
    REMOTE_PORT: int = 22
    REMOTE_USER: str = "user"
    REMOTE_PASSWORD: str = "password"
    REMOTE_BASE_PATH: str = "/videos"
    
    # FFmpeg Configuration
    FFMPEG_PATH: str = "ffmpeg"
    FFPROBE_PATH: str = "ffprobe"

    # Local Storage Configuration
    STORAGE_PATH: str = os.path.join(os.getcwd(), "storage")
    DOWNLOAD_PATH: str = os.path.join(STORAGE_PATH, "downloads")
    OUTPUT_PATH: str = os.path.join(STORAGE_PATH, "output")

    # DB Configuration
    DATABASE_URL: str = "sqlite:///./videos.db"

    # Backend Configurations
    SM_BACKEND_URL: str = "http://localhost:8001"
    AI_CONTROLLER_URL: str = "http://localhost:8002"
    CUT_VIDEO_URL: str = "http://localhost:8000"

    class Config:
        env_file = ".env"

settings = Settings()

# Ensure directories exist
os.makedirs(settings.DOWNLOAD_PATH, exist_ok=True)
os.makedirs(settings.OUTPUT_PATH, exist_ok=True)
