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

    def process_day_room(self, task_id: int, skip_sync: bool = False):
        db = SessionLocal()
        task = db.query(ProcessingTask).get(task_id)
        if not task:
            return

        try:
            task.status = TaskStatus.DOWNLOADING.value
            task.logs = task.logs + [f"Starting sync for {task.date_str} {task.room}"]
            db.commit()

            # 1. Sync files
            local_files = []
            if not skip_sync:
                local_files = self.remote_sync.sync_room_videos(task.date_str, task.room)
            else:
                room_dir = os.path.join(settings.DOWNLOAD_PATH, task.date_str, task.room)
                if os.path.exists(room_dir):
                    local_files = [os.path.join(room_dir, f) for f in os.listdir(room_dir) if f.endswith(('.mp4', '.mkv', '.avi'))]

            if not local_files:
                task.status = TaskStatus.FAILED.value
                task.logs = task.logs + ["No files found to process."]
                db.commit()
                return

            # Deduplicate files by size (identical copies have same byte count)
            seen_sizes = set()
            unique_files = []
            for f in sorted(local_files):
                fsize = os.path.getsize(f)
                if fsize not in seen_sizes:
                    seen_sizes.add(fsize)
                    unique_files.append(f)
                else:
                    logger.info(f"Skipping duplicate file (same size): {f}")
            local_files = unique_files

            task.status = TaskStatus.PROCESSING.value
            task.logs = task.logs + [f"Found {len(local_files)} files. Starting processing."]
            db.commit()

            # 2. Analyze files (get start time and duration)
            video_meta = []
            date_obj = datetime.strptime(task.date_str, "%Y-%m-%d")
            
            for f in local_files:
                info = self.video_service.get_video_info(f)

                # If start_time could not be extracted from filename or metadata,
                # assume the video is a full-day CCTV recording starting at 00:00
                if not info.get("start_time"):
                    logger.warning(
                        f"Could not extract start_time for {f}. "
                        f"Assuming full-day recording starting at 00:00 for {task.date_str}."
                    )
                    info["start_time"] = (0, 0, 0)

                # Room match: parsed room from filename may differ slightly (e.g. "110" vs "110 xona")
                # In that case allow if task.room starts with or is contained in the parsed room, or vice versa
                parsed_room = (info["room"] or "").strip()
                task_room = (task.room or "").strip()
                rooms_match = (
                    not parsed_room  # no room info means accept all
                    or parsed_room == task_room
                    or parsed_room.startswith(task_room)
                    or task_room.startswith(parsed_room)
                )
                if not rooms_match:
                    logger.warning(f"File {f} room '{parsed_room}' does not match task room '{task_room}'. Skipping.")
                    continue

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

                    # Automatically upload to AI Controller
                    import requests
                    try:
                        def upload_to_ai():
                            # 1. Create session
                            r_sess = requests.post(f"{settings.AI_CONTROLLER_URL}/create-session")
                            r_sess.raise_for_status()
                            session_id = r_sess.json()["session_id"]
                            
                            # 2. Upload video
                            with open(final_path, 'rb') as vf:
                                r_vid = requests.post(
                                    f"{settings.AI_CONTROLLER_URL}/upload-video/{session_id}",
                                    files={"video": (final_name, vf, "video/mp4")}
                                )
                                r_vid.raise_for_status()

                            # 3. Register pending session with sm-backend
                            r_reg = requests.post(
                                f"{settings.SM_BACKEND_URL}/api/analysis/register_session/",
                                json={
                                    "session_id": session_id,
                                    "topic": interval.get('subject', 'Unknown'),
                                    "teacher": interval.get('teacher', 'Unknown'),
                                    "video_file": f"/api/download-video/{session_id}"
                                }
                            )
                            r_reg.raise_for_status()
                            
                            return session_id
                            
                        sid = upload_to_ai()
                        task.logs = task.logs + [f"Successfully uploaded to AI. Session ID: {sid}"]
                    except Exception as ai_e:
                        logger.error(f"Failed to upload to AI: {ai_e}")
                        task.logs = task.logs + [f"Failed to upload to AI: {ai_e}"]

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
