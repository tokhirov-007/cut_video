import os
import logging
import re
import requests
from datetime import datetime, time, timedelta, date as date_type
from .remote_sync import RemoteSyncService
from .video_service import VideoService
from ..models import TaskStatus, ProcessingTask, SessionLocal
from ..config import settings

logger = logging.getLogger(__name__)


def rooms_similar(r1, r2):
    """Fuzzy room comparison: '303', 'Room 303', '303-2' etc."""
    if not r1 or not r2:
        return False
    r1c = re.sub(r'[^0-9a-z]', '', r1.lower())
    r2c = re.sub(r'[^0-9a-z]', '', r2.lower())
    if r1c == r2c:
        return True
    if r1c in r2c or r2c in r1c:
        return True
    m1 = re.match(r'^(\d+)', r1)
    m2 = re.match(r'^(\d+)', r2)
    if m1 and m2 and m1.group(1) == m2.group(1):
        return True
        
    # Продвинутое текстовое сравнение (если OCR прочитал буквы вместо цифр)
    # Например: "AY maruza zali 1" vs "8-maruza zali"
    import difflib
    ratio = difflib.SequenceMatcher(None, r1c, r2c).ratio()
    if ratio > 0.8:
        return True
        
    return False


def fetch_all_schedules():
    """Fetch all schedules from backend. Returns list or empty list."""
    try:
        res = requests.get(f"{settings.SM_BACKEND_URL}/api/schedules/", timeout=10)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        logger.error(f"Could not fetch schedules: {e}")
        return []


def find_intervals_for(schedules, target_date_str, target_room):
    """
    Find lesson intervals for given date+room.
    Returns list of {start, end, teacher, subject}.
    """
    intervals = []
    for s in schedules:
        if str(s.get('date')) == target_date_str and rooms_similar(str(s.get('room')), target_room):
            intervals.append({
                "start": s['start_time'][:5],
                "end": s['end_time'][:5],
                "teacher": s.get('teacher', ''),
                "subject": s.get('subject', ''),
                "plan_file": s.get('plan_file', None),
            })
    return intervals


class OrchestratorService:
    def __init__(self):
        self.remote_sync = RemoteSyncService()
        self.video_service = VideoService()

    # ─────────────────────────────────────────────────────────────────────────
    # NEW: entry point for uploaded files (OSD-first logic)
    # ─────────────────────────────────────────────────────────────────────────
    def process_uploaded_file(self, task_id: int, file_path: str):
        """
        Full pipeline for a user-uploaded video:
        1. Read OSD → get date, time, room
        2. Find schedule intervals for that date+room
        3. If not found → try nearby dates / all rooms
        4. Cut video into lesson segments
        5. Send each segment to AI controller
        """
        db = SessionLocal()
        task = db.get(ProcessingTask, task_id)
        if not task:
            return

        try:
            task.status = TaskStatus.DOWNLOADING.value
            task.logs = ["Fayl qabul qilindi. OSD o'qilmoqda..."]
            db.commit()

            if not os.path.exists(file_path):
                task.status = TaskStatus.FAILED.value
                task.logs = task.logs + [f"Fayl topilmadi: {file_path}"]
                db.commit()
                return

            # ── Step 1: Read OSD from video ───────────────────────────────
            info = self.video_service.get_video_info(file_path)
            duration = info["duration"]
            osd_room = info.get("room")
            start_time_tuple = info.get("start_time")  # (h, m, s) or None
            is_osd = info.get("is_osd_accurate", False)

            log_lines = []

            if is_osd and start_time_tuple:
                h, m, s = start_time_tuple
                log_lines.append(f"OSD muvaffaqiyatli o'qildi: soat {h:02d}:{m:02d}:{s:02d}, xona: {osd_room or '?'}")
            else:
                log_lines.append("OSD o'qib bo'lmadi. Fayl nomi va metadatadan foydalanilmoqda.")

            task.logs = task.logs + log_lines
            db.commit()

            # ── Step 2: Fetch all schedules ───────────────────────────────
            schedules = fetch_all_schedules()
            if not schedules:
                task.status = TaskStatus.FAILED.value
                task.logs = task.logs + ["Backend'dan jadval olib bo'lmadi."]
                db.commit()
                return

            # ── Step 3: Determine date ────────────────────────────────────
            # Priority: OSD date (extracted from OSD datetime) > form date > today
            osd_date_str = None
            # osd_extractor returns full datetime — we extract the date from file mtime
            # since get_video_info only returns (h,m,s), we infer the date differently
            # Try to get it from OSD via direct extraction
            try:
                from .osd_extractor import extract_osd_info
                osd_dt, osd_room_raw = extract_osd_info(
                    file_path,
                    ffmpeg_exe=VideoService._get_ffmpeg_exe(),
                    duration_sec=duration,
                )
                if osd_dt:
                    osd_date_str = osd_dt.strftime("%Y-%m-%d")
                    if not osd_room:
                        osd_room = osd_room_raw
            except Exception:
                pass

            # Build candidate dates to try (OSD date first, then all schedule dates)
            all_schedule_dates = sorted(
                set(str(s.get('date')) for s in schedules),
                reverse=True
            )

            candidate_dates = []
            if osd_date_str:
                candidate_dates.append(osd_date_str)
            if task.date_str:
                candidate_dates.append(task.date_str)
            # Add all schedule dates (nearest to OSD date first)
            remaining = [d for d in all_schedule_dates if d not in candidate_dates]
            if osd_date_str:
                remaining.sort(key=lambda d: abs(
                    (datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(osd_date_str, "%Y-%m-%d")).days
                ))
            candidate_dates.extend(remaining)

            # ── Step 4: Find matching intervals ───────────────────────────
            # Build candidate rooms list
            candidate_rooms = []
            if task.room:
                candidate_rooms.append(task.room)
            if osd_room:
                candidate_rooms.append(osd_room)
            # Also collect all unique rooms from schedules
            all_rooms = list(set(str(s.get('room')) for s in schedules))
            candidate_rooms.extend(r for r in all_rooms if r not in candidate_rooms)

            found_date = None
            found_room = None
            found_intervals = []

            video_start_time = None
            if start_time_tuple:
                h_v, m_v, s_v = start_time_tuple
                video_start_time = time(h_v, m_v, s_v)

            # First pass: strict overlap check
            # Second pass: fallback to any schedule for this date (least precise)
            for strict_mode in [True, False]:
                for cdate in candidate_dates:
                    # If user specified a date, don't look at other dates in strict mode
                    if strict_mode and task.date_str and cdate != task.date_str:
                        continue
                        
                    for croom in candidate_rooms:
                        # If user specified a room, don't look at other rooms in strict mode
                        if strict_mode and task.room and not rooms_similar(croom, task.room):
                            continue
                            
                        intervals = find_intervals_for(schedules, cdate, croom)
                        if not intervals:
                            continue
                            
                        # Filter out placeholder full-day intervals
                        real_intervals = [
                            iv for iv in intervals
                            if not (iv["start"] == "00:00" and iv["end"] == "23:59")
                        ]
                        if not real_intervals:
                            continue

                        if strict_mode and video_start_time:
                            # Accurate overlap check
                            date_obj_c = datetime.strptime(cdate, "%Y-%m-%d")
                            v_start_dt = datetime.combine(date_obj_c, video_start_time)
                            v_end_dt = v_start_dt + timedelta(seconds=duration)
                            
                            matching_intervals = []
                            for iv in real_intervals:
                                iv_start_dt = datetime.combine(date_obj_c, datetime.strptime(iv["start"], "%H:%M").time())
                                iv_end_dt = datetime.combine(date_obj_c, datetime.strptime(iv["end"], "%H:%M").time())
                                
                                # Check for overlap
                                if max(v_start_dt, iv_start_dt) < min(v_end_dt, iv_end_dt):
                                    matching_intervals.append(iv)
                            
                            if matching_intervals:
                                found_date = cdate
                                found_room = croom
                                found_intervals = matching_intervals
                                break
                        elif not strict_mode:
                            # Loose fallback: just pick the first room that has a schedule on this candidate date
                            found_date = cdate
                            found_room = croom
                            found_intervals = real_intervals
                            break
                            
                    if found_intervals:
                        break
                if found_intervals:
                    break

            if not found_intervals:
                task.status = TaskStatus.FAILED.value
                unknown = "noma'lum"
                task.logs = task.logs + [
                    f"Bu video uchun mos dars jadvali topilmadi. "
                    f"(OSD xona: {osd_room or unknown}, OSD sana: {osd_date_str or unknown})"
                ]
                db.commit()
                return

            task.date_str = found_date
            task.room = found_room
            task.intervals = found_intervals
            task.logs = task.logs + [
                f"Jadval topildi: {found_date}, xona {found_room}, {len(found_intervals)} ta dars."
            ]
            db.commit()

            # Move file to proper location: downloads/<date>/<room>/
            room_dir = os.path.join(settings.DOWNLOAD_PATH, found_date, found_room)
            os.makedirs(room_dir, exist_ok=True)
            final_file = os.path.join(room_dir, os.path.basename(file_path))
            if file_path != final_file:
                os.rename(file_path, final_file)
            file_path = final_file

            # ── Step 5: Cut + upload ──────────────────────────────────────
            self._cut_and_upload(db, task, [file_path], found_date, found_room, found_intervals)

        except Exception as e:
            logger.exception("Error in process_uploaded_file")
            task.status = TaskStatus.FAILED.value
            task.logs = task.logs + [f"Xato: {str(e)}"]
            db.commit()
        finally:
            db.close()

    # ─────────────────────────────────────────────────────────────────────────
    # EXISTING: entry point for scheduled/sync tasks
    # ─────────────────────────────────────────────────────────────────────────
    def process_day_room(self, task_id: int, skip_sync: bool = False):
        db = SessionLocal()
        task = db.get(ProcessingTask, task_id)
        if not task:
            return

        try:
            task.status = TaskStatus.DOWNLOADING.value
            task.logs = task.logs + [f"Starting sync for {task.date_str} {task.room}"]
            db.commit()

            local_files = []
            if not skip_sync:
                local_files = self.remote_sync.sync_room_videos(task.date_str, task.room)
            else:
                task_dir = os.path.join(settings.DOWNLOAD_PATH, task.date_str, task.room)
                if os.path.exists(task_dir):
                    local_files = [
                        os.path.join(task_dir, f)
                        for f in os.listdir(task_dir)
                        if f.endswith(('.mp4', '.mkv', '.avi'))
                    ]

            if not local_files:
                task.status = TaskStatus.FAILED.value
                task.logs = task.logs + ["No files found to process."]
                db.commit()
                return

            # Deduplicate by size
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

            date_obj = datetime.strptime(task.date_str, "%Y-%m-%d")
            video_meta = self._build_video_meta(local_files, date_obj, task.room)

            self._cut_and_upload(db, task, local_files, task.date_str, task.room, task.intervals,
                                  video_meta_override=video_meta)

        except Exception as e:
            logger.exception("Error processing task")
            task.status = TaskStatus.FAILED.value
            task.logs = task.logs + [f"Error: {str(e)}"]
            db.commit()
        finally:
            db.close()

    # ─────────────────────────────────────────────────────────────────────────
    # SHARED helpers
    # ─────────────────────────────────────────────────────────────────────────
    def _build_video_meta(self, local_files, date_obj, task_room):
        """Collect start/end times for each video file using OSD or fallbacks."""
        video_meta = []
        for f_path in local_files:
            f_name = os.path.basename(f_path)
            info = self.video_service.get_video_info(f_path, target_date=date_obj)

            if not info["start_time"]:
                logger.warning(f"Could not determine start time for {f_name}. Skipping.")
                continue

            parsed_room = (info["room"] or "").strip()
            # If no room from OSD/filename, or if it doesn't match, trust the backend task room anyway
            if parsed_room and not rooms_similar(parsed_room, task_room):
                logger.warning(f"File {f_name} room '{parsed_room}' != task room '{task_room}'. Trusting task room anyway.")

            h, m, s = info["start_time"]
            video_start = datetime.combine(date_obj, time(h, m, s))
            video_end = video_start + timedelta(seconds=info["duration"])

            video_meta.append({
                "path": f_path,
                "name": f_name,
                "start": video_start,
                "end": video_end,
                "is_osd": info.get("is_osd_accurate", False),
            })
            logger.info(f"Video: {f_name} | {video_start.time()} – {video_end.time()} | OSD={info.get('is_osd_accurate')}")

        video_meta.sort(key=lambda x: x["start"])
        return video_meta

    def _cut_and_upload(self, db, task, local_files, date_str, room, intervals,
                         video_meta_override=None):
        """Cut video by lesson intervals and upload each to AI controller."""
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")

        if video_meta_override is not None:
            video_meta = video_meta_override
        else:
            video_meta = self._build_video_meta(local_files, date_obj, room)

        if not video_meta:
            task.status = TaskStatus.FAILED.value
            task.logs = task.logs + ["Video fayllari vaqtini aniqlab bo'lmadi."]
            db.commit()
            return

        task.status = TaskStatus.PROCESSING.value
        db.commit()

        output_room_dir = os.path.join(settings.OUTPUT_PATH, date_str, room)
        os.makedirs(output_room_dir, exist_ok=True)

        any_cut = False
        for interval in intervals:
            i_start_t = datetime.strptime(interval["start"], "%H:%M").time()
            i_end_t = datetime.strptime(interval["end"], "%H:%M").time()
            i_start_dt = datetime.combine(date_obj, i_start_t)
            i_end_dt = datetime.combine(date_obj, i_end_t)

            segments = []
            for i, meta in enumerate(video_meta):
                overlap_start = max(i_start_dt, meta["start"])
                overlap_end = min(i_end_dt, meta["end"])
                if overlap_start < overlap_end:
                    start_offset = (overlap_start - meta["start"]).total_seconds()
                    seg_dur = (overlap_end - overlap_start).total_seconds()
                    seg_path = os.path.join(
                        output_room_dir,
                        f"temp_{i_start_t.strftime('%H%M')}_{i}.mp4"
                    )
                    self.video_service.cut_segment(meta["path"], start_offset, seg_dur, seg_path)
                    segments.append(seg_path)

            if segments:
                any_cut = True
                teacher = interval.get('teacher', '')
                subject = interval.get('subject', '')
                final_name = (
                    f"{date_str}_{room}_"
                    f"{interval['start'].replace(':', '-')}_"
                    f"{interval['end'].replace(':', '-')}.mp4"
                )
                final_path = os.path.join(output_room_dir, final_name)
                self.video_service.merge_segments(segments, final_path)

                for seg in segments:
                    if os.path.exists(seg):
                        os.remove(seg)

                task.logs = task.logs + [
                    f"✓ {interval['start']}–{interval['end']} | {subject} | {teacher}"
                ]
                db.commit()

                # Upload to AI
                self._upload_to_ai(db, task, final_path, final_name, interval, room, date_str)
            else:
                task.logs = task.logs + [
                    f"✗ {interval['start']}–{interval['end']}: video kesimi topilmadi"
                ]
                db.commit()

        if not any_cut:
            task.status = TaskStatus.FAILED.value
            task.logs = task.logs + ["Hech qaysi dars uchun video topilmadi."]
        else:
            task.status = TaskStatus.COMPLETED.value
        db.commit()

    def _upload_to_ai(self, db, task, final_path, final_name, interval, room, date_str):
        """Extract audio and upload to AI controller + register with backend."""
        audio_name = final_name.replace(".mp4", ".mp3")
        audio_path = final_path.replace(".mp4", ".mp3")
        try:
            task.logs = task.logs + [f"Audio ajratilmoqda: {final_name}"]
            db.commit()
            self.video_service.extract_audio(final_path, audio_path)

            # Create session
            r_sess = requests.post(f"{settings.AI_CONTROLLER_URL}/create-session", timeout=30)
            r_sess.raise_for_status()
            session_id = r_sess.json()["session_id"]

            # Upload audio
            with open(audio_path, 'rb') as af:
                r_aud = requests.post(
                    f"{settings.AI_CONTROLLER_URL}/api/upload-media/{session_id}",
                    files={"video": (audio_name, af, "audio/mpeg")},
                    timeout=300,
                )
                r_aud.raise_for_status()

            # Upload notes (plan) if available
            plan_url = interval.get('plan_file')
            if plan_url:
                try:
                    # Download the plan file from backend to pass to AI controller
                    clean_url = plan_url if plan_url.startswith('http') else f"{settings.SM_BACKEND_URL}{plan_url}"
                    plan_resp = requests.get(clean_url, timeout=30)
                    if plan_resp.status_code == 200:
                        import tempfile
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_plan:
                            tmp_plan.write(plan_resp.content)
                            tmp_plan_path = tmp_plan.name
                        
                        with open(tmp_plan_path, 'rb') as f_plan:
                            r_notes = requests.post(
                                f"{settings.AI_CONTROLLER_URL}/upload-notes/{session_id}",
                                files={"notes_file": (os.path.basename(plan_url), f_plan, "application/pdf")},
                                data={"teacher": interval.get('teacher', 'Unknown'), "topic": interval.get('subject', 'Unknown')},
                                timeout=60
                            )
                        os.remove(tmp_plan_path)
                        if r_notes.status_code == 200:
                            task.logs = task.logs + ["Dars ishlanmasi (reja) AI ga yuborildi."]
                except Exception as ex_plan:
                    logger.warning(f"Failed to upload notes: {ex_plan}")
                    task.logs = task.logs + [f"Rejani yuborishda xato: {str(ex_plan)[:50]}"]

            # Register with backend
            # Use CUT_VIDEO_URL to provide a direct link to the actual video file
            video_filename = os.path.basename(final_path)
            public_video_url = f"{settings.CUT_VIDEO_URL}/download/{date_str}/{room}/{video_filename}"

            r_reg = requests.post(
                f"{settings.SM_BACKEND_URL}/api/analysis/register_session/",
                json={
                    "session_id": session_id,
                    "topic": interval.get('subject', 'Unknown'),
                    "teacher": interval.get('teacher', 'Unknown'),
                    "room": room,
                    "video_file": public_video_url,
                },
                timeout=30,
            )
            r_reg.raise_for_status()

            # Trigger background analysis in AI Controller with OSD metadata
            try:
                # OSD start time (interval['start']) is the best indicator for schedule matching
                r_start = requests.get(
                    f"{settings.AI_CONTROLLER_URL}/analyze-background/{session_id}",
                    params={
                        "date": date_str,
                        "time": interval.get('start', '09:00'),
                        "room": room,
                        "video_url": public_video_url
                    },
                    timeout=30
                )
                if r_start.status_code == 200:
                    task.logs = task.logs + [f"AI analizi avtomatik boshlandi. Session: {session_id}"]
                else:
                    task.logs = task.logs + [f"AI analizini boshlashda xato: {r_start.status_code}"]
            except Exception as start_e:
                task.logs = task.logs + [f"AI analizini boshlashda xato (Tarmoq): {str(start_e)[:50]}"]

            if os.path.exists(audio_path):
                os.remove(audio_path)

            db.commit()

        except Exception as ai_e:
            logger.error(f"Failed to upload to AI: {ai_e}")
            task.logs = task.logs + [f"AI xato: {ai_e}"]
            db.commit()
