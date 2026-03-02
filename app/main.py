from fastapi import FastAPI, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
import os
from datetime import datetime

from .database import get_db
from .models import ProcessingTask, TaskStatus, init_db
from .services.orchestrator import OrchestratorService
from .config import settings

import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI(title="CCTV Video Processing API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")

orchestrator = OrchestratorService()

@app.on_event("startup")
def startup_event():
    init_db()

@app.post("/process-day")
async def process_day(
    data: dict, 
    background_tasks: BackgroundTasks, 
    db: Session = Depends(get_db)
):
    """
    Example payload:
    {
      "date": "2026-02-24",
      "rooms": ["room_1", "room_2", "room_3"],
      "intervals": [{"start": "09:30", "end": "10:20"}]
    }
    """
    date_str = data.get("date")
    rooms = data.get("rooms", [])
    intervals = data.get("intervals", [])

    task_ids = []
    for room in rooms:
        task = ProcessingTask(
            date_str=date_str,
            room=room,
            intervals=intervals,
            status=TaskStatus.PENDING.value
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_ids.append(task.id)
        background_tasks.add_task(orchestrator.process_day_room, task.id)

    return {"message": "Processing started", "task_ids": task_ids}

@app.get("/status/{date}")
async def get_status(date: str, db: Session = Depends(get_db)):
    tasks = db.query(ProcessingTask).filter(ProcessingTask.date_str == date).all()
    return [{
        "room": t.room,
        "status": t.status,
        "logs": t.logs,
        "updated_at": t.updated_at
    } for t in tasks]

@app.get("/videos/{date}/{room}")
async def list_videos(date: str, room: str):
    date = date.strip()
    room = room.strip()
    room_path = os.path.join(settings.OUTPUT_PATH, date, room)
    if not os.path.exists(room_path):
        return []
    return os.listdir(room_path)

@app.get("/download/{date}/{room}/{filename}")
async def download_video(date: str, room: str, filename: str):
    date = date.strip()
    room = room.strip()
    file_path = os.path.join(settings.OUTPUT_PATH, date, room, filename)
    if not os.path.exists(file_path):
        logger.warning(f"Download failed: file not found at {file_path}")
        raise HTTPException(status_code=404, detail="File not found")
    
    logger.info(f"Serving file for download: {file_path} (size: {os.path.getsize(file_path)} bytes)")
    return FileResponse(file_path)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
