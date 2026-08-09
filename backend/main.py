"""
main.py

MVP API for Filmify. No auth, no billing — just upload, process, download.

Job state lives in memory (dict) for the MVP. This is fine for a single
instance / low traffic. See README.md "Scaling past the MVP" for the
swap to Redis + a real task queue (Celery/RQ) + object storage (S3)
when this needs to survive restarts or run on multiple workers.
"""

import shutil
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pipeline import JobOptions, run_pipeline, LOOK_PRESETS

BASE_DIR = Path(__file__).parent
STORAGE_DIR = BASE_DIR / "storage"
UPLOADS_DIR = STORAGE_DIR / "uploads"
DEFAULT_OUTPUTS_DIR = Path.home() / "Movies" / "Filmify"
FRONTEND_DIR = BASE_DIR.parent / "frontend"

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Filmify API")

# In-memory job store: {job_id: {"status": ..., "error": ..., "output": ...}}
JOBS: dict[str, dict] = {}

MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500MB cap for MVP


class JobStatus(BaseModel):
    job_id: str
    status: Literal["queued", "processing", "done", "error"]
    error: str | None = None


@app.get("/api/options")
async def get_options():
    return {"looks": list(LOOK_PRESETS.keys())}


@app.post("/api/jobs", response_model=JobStatus)
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    look: str = Form("warm_film"),
    grain_intensity: int = Form(15),
    target_fps: int = Form(24),
    denoise_audio: bool = Form(True),
    output_dir: str = Form(""),
):
    if look not in LOOK_PRESETS:
        raise HTTPException(400, f"Unknown look '{look}'. Choose from {list(LOOK_PRESETS)}")

    if output_dir.strip():
        out_dir = Path(output_dir.strip()).expanduser() / "filmify"
    else:
        out_dir = DEFAULT_OUTPUTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4().hex
    original_stem = Path(file.filename).stem if file.filename else job_id
    input_path = UPLOADS_DIR / f"{job_id}_{file.filename}"
    output_path = out_dir / f"{original_stem}_filmify.mp4"

    size = 0
    with input_path.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                input_path.unlink(missing_ok=True)
                raise HTTPException(413, "File too large (500MB limit for MVP)")
            f.write(chunk)

    options = JobOptions(
        look=look,
        grain_intensity=grain_intensity,
        target_fps=target_fps,
        denoise_audio=denoise_audio,
    )

    JOBS[job_id] = {"status": "queued", "error": None, "output": None}
    background_tasks.add_task(process_job, job_id, input_path, output_path, options)

    return JobStatus(job_id=job_id, status="queued")


async def process_job(job_id: str, input_path: Path, output_path: Path, options: JobOptions):
    JOBS[job_id]["status"] = "processing"
    try:
        await run_pipeline(input_path, output_path, options)
        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["output"] = str(output_path)
    except Exception as exc:  # noqa: BLE001 — surface any failure to the client
        JOBS[job_id]["status"] = "error"
        JOBS[job_id]["error"] = str(exc)
    finally:
        input_path.unlink(missing_ok=True)


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return JobStatus(job_id=job_id, status=job["status"], error=job["error"])


@app.get("/api/jobs/{job_id}/download")
async def download_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != "done":
        raise HTTPException(409, f"Job is not ready (status: {job['status']})")
    output_path = Path(job["output"])
    return FileResponse(str(output_path), media_type="video/mp4", filename=output_path.name)


# Serve the frontend last so /api/* routes above take priority.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
