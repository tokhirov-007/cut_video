import os
import logging
from datetime import datetime, time, timedelta
from .remote_sync import RemoteSyncService
from .video_service import VideoService
from ..models import TaskStatus, ProcessingTask, SessionLocal
from ..config import settings

logger = logging.getLogger(__name__)

class OrchestratorService:
    def __init__(self):
        self.remote_sync = RemoteSyncService()
        self.video_service = VideoService()

    def process_day_room(self, task_id: int):
        db = SessionLocal()
        task = db.query(ProcessingTask).get(task_id)
        if not task:
            return

        try:
            task.status = TaskStatus.DOWNLOADING.value
            task.logs = task.logs + [f"Starting sync for {task.date_str} {task.room}"]
            db.commit()

            # 1. Sync files
            local_files = self.remote_sync.sync_room_videos(task.date_str, task.room)
            if not local_files:
                task.status = TaskStatus.FAILED.value
                task.logs = task.logs + ["No files found on remote server or sync failed."]
                db.commit()
                return

            task.status = TaskStatus.PROCESSING.value
            task.logs = task.logs + [f"Downloaded {len(local_files)} files. Starting processing."]
            db.commit()

            # 2. Analyze files (get start time and duration)
            video_meta = []
            date_obj = datetime.strptime(task.date_str, "%Y-%m-%d")
            
            for f in local_files:
                info = self.video_service.get_video_info(f)
                
                # Check if file belongs to the correct room
                if info["room"] and info["room"] != task.room:
                    logger.warning(f"File {f} room {info['room']} does not match task room {task.room}. Skipping.")
                    continue

                if info["start_time"]:
                    # info["start_time"] is (h, m, s)
                    start_dt = datetime.combine(date_obj, time(*info["start_time"]))
                    duration = timedelta(seconds=info["duration"])
                    video_meta.append({
                        "path": f,
                        "start": start_dt,
                        "end": start_dt + duration,
                        "duration": info["duration"]
                    })
            
            # Sort by start time
            video_meta.sort(key=lambda x: x["start"])

            # 3. Process each interval
            output_room_dir = os.path.join(settings.OUTPUT_PATH, task.date_str, task.room)
            os.makedirs(output_room_dir, exist_ok=True)

            for interval in task.intervals:
                # interval e.g. {"start": "09:30", "end": "10:20"}
                i_start_t = datetime.strptime(interval["start"], "%H:%M").time()
                i_end_t = datetime.strptime(interval["end"], "%H:%M").time()
                
                i_start_dt = datetime.combine(date_obj, i_start_t)
                i_end_dt = datetime.combine(date_obj, i_end_t)

                segments = []
                for i, meta in enumerate(video_meta):
                    # Check overlap
                    overlap_start = max(i_start_dt, meta["start"])
                    overlap_end = min(i_end_dt, meta["end"])

                    if overlap_start < overlap_end:
                        # Overlap exists
                        start_offset = (overlap_start - meta["start"]).total_seconds()
                        duration = (overlap_end - overlap_start).total_seconds()
                        
                        seg_path = os.path.join(output_room_dir, f"temp_{i_start_t.strftime('%H%M')}_{i}.mp4")
                        self.video_service.cut_segment(meta["path"], start_offset, duration, seg_path)
                        segments.append(seg_path)

                if segments:
                    final_name = f"{task.date_str}_{task.room}_{interval['start'].replace(':', '-')}_{interval['end'].replace(':', '-')}.mp4"
                    final_path = os.path.join(output_room_dir, final_name)
                    self.video_service.merge_segments(segments, final_path)
                    
                    # Cleanup temp segments
                    for s in segments:
                        if os.path.exists(s):
                            os.remove(s)
                    
                    task.logs = task.logs + [f"Completed interval {interval['start']}-{interval['end']}"]
                else:
                    task.logs = task.logs + [f"Gap: No video for interval {interval['start']}-{interval['end']}"]
                
                db.commit()

            task.status = TaskStatus.COMPLETED.value
            db.commit()

        except Exception as e:
            logger.exception("Error processing task")
            task.status = TaskStatus.FAILED.value
            task.logs = task.logs + [f"Error: {str(e)}"]
            db.commit()
        finally:
            db.close()
