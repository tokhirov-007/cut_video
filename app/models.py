from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import enum
import datetime
from .config import settings

Base = declarative_base()

class TaskStatus(enum.Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class ProcessingTask(Base):
    __tablename__ = "processing_tasks"

    id = Column(Integer, primary_key=True, index=True)
    date_str = Column(String, index=True)  # YYYY-MM-DD
    room = Column(String, index=True)
    intervals = Column(JSON)  # List of dicts {"start": "HH:MM", "end": "HH:MM"}
    status = Column(String, default=TaskStatus.PENDING.value)
    logs = Column(JSON, default=[])  # List of log messages
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

engine = create_engine(settings.DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    Base.metadata.create_all(bind=engine)
