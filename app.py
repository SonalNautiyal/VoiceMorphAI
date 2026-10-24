"""VoiceMorph AI — FastAPI backend.

Endpoints
  GET  /api/health                     ffmpeg + API key status
  GET  /api/voices                     voice card data
  GET  /api/voices/{id}/preview        short Drama 3 sample (cached)
  POST /api/jobs                       upload a clip -> starts analysis
  GET  /api/jobs                       recent jobs (history)
  GET  /api/jobs/{id}                  stage status / result / error (poll)
  POST /api/jobs/{id}/transform        voice setup + transcript -> takes + mux
  POST /api/jobs/{id}/select-take      swap the exported take (no new TTS)
  /files/...                           job artifacts (original, takes, final)

Nothing here exposes the API key to the browser — the frontend only talks to
this server.
"""
import re
import threading
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import agents.coordinator as coordinator
import agents.voice_agent as voice_agent
import services.media as media
from agents.coordinator import job_view
from config import DATA_DIR, FISH_API_KEY, MAX_DURATION_S, MAX_UPLOAD_MB, STATIC_DIR, public_voices
from services.fish_api import FishAPIError

app = FastAPI(title="VoiceMorph AI")

# In-memory cache of the short voice samples so the demo doesn't cost on every click.
_preview_cache: dict[str, bytes] = {}
_JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")
MAX_TRANSCRIPT_CHARS = 20_000


@app.get("/api/health")
def health():
    missing = []
    if not FISH_API_KEY:
        missing.append("FISH_API_KEY (in .env)")
    if not media.ffmpeg_available():
        missing.append("ffmpeg (on PATH)")
    return {
        "ready": not missing,
        "ffmpeg": media.ffmpeg_available(),
        "api_key": bool(FISH_API_KEY),
        "missing": missing,
        "limits": {"max_upload_mb": MAX_UPLOAD_MB, "max_duration_s": MAX_DURATION_S},
    }


@app.get("/api/voices")
def list_voices():
    return public_voices()


@app.get("/api/voices/{voice_id}/preview")
def voice_preview(voice_id: str):
    cached = _preview_cache.get(voice_id)
    if cached:
        return Response(content=cached, media_type="audio/mpeg")
    try:
        mime, audio = voice_agent.preview_audio(voice_id)
    except voice_agent.VoiceAgentError:
        return JSONResponse(status_code=404, content={"message": f"Unknown voice '{voice_id}'."})
    except FishAPIError as exc:
        return JSONResponse(status_code=503, content={"message": exc.friendly})
    _preview_cache[voice_id] = audio
    return Response(content=audio, media_type=mime)


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...)):
    """Save the upload and start the analysis pipeline in a background thread."""
    filename = file.filename or ""
    try:
        data = await file.read()
        coordinator.media_agent.validate_file(filename, len(data))
    except coordinator.media_agent.MediaAgentError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    job = coordinator.new_job()
    upload_path = Path(job["dir"]) / f"upload{Path(filename).suffix.lower()}"
    upload_path.write_bytes(data)

    threading.Thread(
        target=coordinator.run_analyze,
        args=(job, upload_path, filename, len(data)),
        daemon=True,
    ).start()
    return {"id": job["id"]}


@app.get("/api/jobs")
def list_jobs():
    """Recent jobs, newest first — powers the lightweight history panel."""
    return coordinator.history_view()


@app.get("/api/jobs/{job_id}")
def get_job_status(job_id: str):
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job id.")
    job = coordinator.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found — it may have been created before the server restarted.")
    return job_view(job)


class TransformRequest(BaseModel):
    voice_id: str = "atlas"
    transcript: str | None = None
    # Multi-speaker: original <|speaker:N|> label -> voice profile id.
    speaker_voices: dict[str, str] = Field(default_factory=dict)
    # How many Drama 3 takes to generate and score (1-3).
    takes: int = 1
    # User pacing multiplier (0.8-1.25) applied on top of each voice's preset.
    speed: float = 1.0


@app.post("/api/jobs/{job_id}/transform")
def transform_job(job_id: str, req: TransformRequest):
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job id.")
    job = coordinator.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "done" or not job["result"].get("text"):
        raise HTTPException(
            status_code=409,
            detail="The analysis step hasn't finished successfully yet. Wait for the transcript before transforming.",
        )

    transcript = (req.transcript or "").strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="The transcript is empty. Type the dialogue you want spoken.")
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise HTTPException(status_code=400, detail=f"The transcript is too long (limit {MAX_TRANSCRIPT_CHARS} characters).")
    if not 0.5 <= req.speed <= 2.0:
        raise HTTPException(status_code=400, detail="Speed must be between 0.5 and 2.0.")
    takes = max(1, min(3, req.takes))

    threading.Thread(
        target=coordinator.run_transform,
        args=(job, req.voice_id, transcript, req.speaker_voices, takes, req.speed),
        daemon=True,
    ).start()
    return job_view(job)


class SelectTakeRequest(BaseModel):
    take: int


@app.post("/api/jobs/{job_id}/select-take")
def select_take(job_id: str, req: SelectTakeRequest):
    """Export a different (already generated) scoreboard take — no new TTS cost."""
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job id.")
    job = coordinator.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "done" or not (job["result"].get("takes") or []):
        raise HTTPException(status_code=409, detail="No completed takes to choose from yet.")

    existing = {t["take"] for t in job["result"]["takes"]}
    if req.take not in existing:
        raise HTTPException(status_code=400, detail=f"Take {req.take} wasn't generated for this job.")

    threading.Thread(target=coordinator.run_select_take, args=(job, req.take), daemon=True).start()
    return job_view(job)


# Static artifacts (originals + generated media) and the UI itself.
app.mount("/files", StaticFiles(directory=DATA_DIR), name="files")
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
