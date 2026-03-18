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

# Configure logging
LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
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

from fastapi import File, UploadFile, Form
import shutil, uuid

@app.post("/upload-and-process")
async def upload_and_process(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    date: str = Form(None),
    db: Session = Depends(get_db)
):
    """
    Saves the uploaded video and queues it for processing.
    OSD reading + schedule matching + cutting all happen inside the orchestrator.
    """
    try:
        # Save uploaded file to a neutral temp folder (named by UUID to avoid conflicts)
        upload_id = str(uuid.uuid4())
        upload_dir = os.path.join(settings.DOWNLOAD_PATH, "uploads", upload_id)
        os.makedirs(upload_dir, exist_ok=True)
        file_path = os.path.join(upload_dir, video.filename)

        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(video.file, buffer)

        # Create a bare task — orchestrator will fill date/room/intervals after reading OSD
        task = ProcessingTask(
            date_str=date or "",
            room="",
            intervals=[],
            status=TaskStatus.PENDING.value,
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        # Queue background processing
        background_tasks.add_task(
            orchestrator.process_uploaded_file,
            task.id,
            file_path,
        )

        return {
            "message": f"'{video.filename}' muvaffaqiyatli yuklandi. OSD analizlanmoqda va dars jadvali qidirilmoqda...",
            "task_id": task.id,
        }
    except Exception as e:
        logger.error(f"Failed to process uploaded video: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/fetch-and-process")
async def fetch_and_process(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Fetches today's schedule from sm-backend and creates processing tasks automatically.
    """
    import requests
    try:
        # Assuming we might need auth or just a public endpoint. For now assuming it's accessible locally:
        # Note: You should add an API key or auth header in production
        response = requests.get(f"{settings.SM_BACKEND_URL}/api/schedules/today/")
        response.raise_for_status()
        schedules = response.json()
        
        if not schedules:
            return {"message": "No schedules found for today."}

        # Group schedules by room
        room_schedules = {}
        for s in schedules:
            room = s['room']
            if room not in room_schedules:
                room_schedules[room] = []
            room_schedules[room].append({
                "start": s['start_time'][:5],
                "end": s['end_time'][:5],
                "teacher": s['teacher'],
                "subject": s['subject']
            })

        today_str = datetime.today().strftime('%Y-%m-%d')
        task_ids = []
        
        for room, intervals in room_schedules.items():
            task = ProcessingTask(
                date_str=today_str,
                room=room,
                intervals=intervals,
                status=TaskStatus.PENDING.value
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            task_ids.append(task.id)
            background_tasks.add_task(orchestrator.process_day_room, task.id)

        return {
            "message": f"Successfully fetched schedule and queued {len(task_ids)} room tasks.",
            "task_ids": task_ids
        }

    except Exception as e:
        logger.error(f"Failed to fetch and process schedule: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch schedule: {str(e)}")

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

@app.get("/tasks/{task_id}")
async def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(ProcessingTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "id": task.id,
        "room": task.room,
        "status": task.status,
        "logs": task.logs,
        "updated_at": task.updated_at
    }

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
