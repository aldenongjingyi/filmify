# Filmify — MVP

Turns a raw iPhone clip into a graded, cropped, grained, Instagram-ready
video. One container, no auth, no billing — upload, process, download.

## Architecture (MVP)

```
Browser (frontend/index.html)
     │  POST /api/jobs  (multipart: file + grading options)
     ▼
FastAPI (backend/main.py)
     │  saves upload to storage/uploads/
     │  kicks off background task
     ▼
pipeline.py  →  ffmpeg subprocess
     │  fps convert → crop → color grade → grain → scale → encode
     ▼
storage/outputs/{job_id}.mp4
     │
     ▼
Browser polls GET /api/jobs/{id}, then GET /api/jobs/{id}/download
```

Everything — API, job processing, static frontend — runs in **one
container**. Job state lives in an in-memory dict. This is intentional
for an MVP: it's the fastest path to something real users can hit, and
the whole thing is one `docker run` away from being live.

## Local dev

Requires Docker (it bundles FFmpeg for you, no local install needed).

```bash
docker compose up --build
```

Then open http://localhost:8000

Without Docker, if you have `ffmpeg` installed locally:

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload
```

## Deployment (pick one)

All of these work because it's a single Dockerfile — no special build
config needed beyond "deploy this container."

### Railway (simplest)
1. Push this folder to a GitHub repo.
2. railway.app → New Project → Deploy from GitHub repo.
3. Railway detects the Dockerfile automatically. Deploy.
4. Add a volume mounted at `/app/backend/storage` if you want uploads/
   outputs to survive restarts (Railway → your service → Volumes).

### Fly.io
```bash
fly launch          # detects Dockerfile, asks a few questions
fly volumes create filmify_storage --size 3
# in fly.toml, mount the volume at /app/backend/storage
fly deploy
```

### Render
1. New → Web Service → connect repo.
2. Render detects the Dockerfile.
3. Add a persistent disk mounted at `/app/backend/storage` (Render →
   Disks) if you want files to survive restarts.

## Known MVP limitations (by design, not oversight)

- **Job state is in-memory.** Restarting the container loses in-flight
  job status (the file itself, if already written, is fine as long as
  storage is on a volume). Fine for early traffic; see below to fix.
- **No cleanup job.** Uploaded/output files accumulate on disk. Add a
  cron (or a simple `asyncio` background loop) that deletes files older
  than N hours.
- **No auth/rate limiting.** Anyone with the URL can upload. Fine for a
  private beta with a handful of users; not fine once you share the
  link publicly. Fastest fix: stick this behind Cloudflare Access, or
  add a simple API key check in `main.py` before opening it up.
- **Single container does all the work.** A big 500MB video will tie up
  that container's CPU during encoding. Fine at low concurrency (a few
  jobs at once, depending on host CPU); becomes the bottleneck once
  multiple people upload simultaneously.

## Scaling past the MVP

When you outgrow the above (real signups, concurrent jobs, need jobs to
survive a restart), the upgrade path is well-worn:

1. **Job queue:** swap `BackgroundTasks` for **Celery** or **RQ** with
   **Redis** as the broker. Same `pipeline.py` logic, just called from a
   worker process instead of in-request.
2. **Storage:** swap local disk for **S3** (or R2/Backblaze — cheaper
   egress). Upload goes straight to a presigned S3 URL from the
   browser; worker pulls from S3, pushes result back to S3; frontend
   downloads via a presigned GET URL.
3. **Job state:** move the `JOBS` dict to **Postgres** or **Redis**, so
   status survives restarts and multiple API instances stay in sync.
4. **Auth + billing:** add a real auth provider (Clerk/Auth0/Supabase
   Auth) and Stripe for a paid tier — you explicitly deferred this for
   the MVP, so it's a clean addition later rather than a rewrite.
5. **Horizontal scale:** once processing is in a queue instead of
   in-request, you can run N worker containers pulling from the same
   Redis queue — GPU-accelerated FFmpeg (NVENC) if encode speed becomes
   the bottleneck.

None of this changes `pipeline.py` — the FFmpeg logic is already
decoupled from the web layer, which is why this path is additive
rather than a rewrite.

## Tuning the look

Grading presets live in `backend/pipeline.py` → `LOOK_PRESETS`, as
native FFmpeg filter chains (no external `.cube` LUT files needed, so
there's nothing to license or manage). Add a new preset by adding a key
to that dict and a matching `<option>` in `frontend/index.html`.
