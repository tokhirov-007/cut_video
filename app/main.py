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

from fastapi import File, UploadFile, Form
import shutil

@app.post("/upload-and-process")
async def upload_and_process(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    date: str = Form(None),
    db: Session = Depends(get_db)
):
    import requests
    import re
    from datetime import datetime
    try:
        # Extract from camera filename dynamically before fetching schedules
        filename = video.filename
        target_date = None
        room_candidate = None
        
        # New format: 504-2_v_bino_0022_0022_20260218135939_20260218150109_1589189.mp4
        new_pattern = r'^([^-/_]+).*?(\d{14})_(\d{14})'
        match_new = re.search(new_pattern, filename)
        if match_new:
            room_candidate = match_new.group(1)
            try:
                file_dt = datetime.strptime(match_new.group(2), "%Y%m%d%H%M%S")
                target_date = file_dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
        else:
            # Old format fallback: room1_12-00.mp4 (no date)
            old_pattern = r'^([^-/_]+)_(\d{2})-(\d{2})'
            match_old = re.search(old_pattern, filename)
            if match_old:
                room_candidate = match_old.group(1)

        # 1. Fetch ALL schedules
        res = requests.get(f"{settings.SM_BACKEND_URL}/api/schedules/")
        res.raise_for_status()
        schedules = res.json()
        
        if not schedules:
            return {"message": "Umuman dars jadvallari topilmadi. Video kesish to'xtatildi."}

        available_dates = sorted(list(set([str(s.get('date')) for s in schedules])), reverse=True)
        
        if not target_date:
            # Fallback chaining if filename has no date:
            fallback_date = date if date else str(datetime.today().date())
            target_date = fallback_date if fallback_date in available_dates else available_dates[0]

        intervals = []
        for s in schedules:
            if str(s.get('date')) == target_date:
                # If room wasn't determined by filename, grab the first room from the matching date
                if not room_candidate:
                    room_candidate = str(s.get('room'))
                
                # Only append intervals that match the room we are analyzing
                if str(s.get('room')) == room_candidate:
                    intervals.append({
                        "start": s['start_time'][:5],
                        "end": s['end_time'][:5],
                        "teacher": s['teacher'],
                        "subject": s['subject']
                    })
        
        if not intervals:
            return {"message": f"{target_date} sanasi uchun dars jadvallari topilmadi. Video kesish to'xtatildi."}
            
        room = room_candidate

        # 2. Save uploaded file to that room's directory
        room_dir = os.path.join(settings.DOWNLOAD_PATH, target_date, room)
        os.makedirs(room_dir, exist_ok=True)
        file_path = os.path.join(room_dir, video.filename)
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(video.file, buffer)
        
        # Sort intervals and find the earliest start time to assume video started recording then
        intervals.sort(key=lambda x: x["start"])
        start_time = intervals[0]["start"]
        
        # 3. Create task and queue it
        task = ProcessingTask(
            date_str=target_date,
            room=room,
            intervals=intervals,
            status=TaskStatus.PENDING.value
        )
        db.add(task)
        db.commit()
        db.refresh(task)

        # Pass skip_sync=True to use the local file we just uploaded
        background_tasks.add_task(orchestrator.process_day_room, task.id, skip_sync=True)
        
        return {
            "message": f"Muvaffaqiyatli yuklandi: {video.filename}. {len(intervals)} ta dars jadvali bo'yicha fon rejimida video kesilishi va analizga yuborilishi boshlandi.",
            "task_id": task.id
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
    task = db.query(ProcessingTask).get(task_id)
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
