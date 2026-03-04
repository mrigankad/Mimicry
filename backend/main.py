"""
Mimicry — FastAPI backend v5

New in v5:
  - SQLite persistent job store (jobs survive server restart)
  - Optional API key auth (set MIMICRY_API_KEY env var)
  - GET /api/voices/{id}/audio   → serve reference WAV for preview
  - MP3 audio URL preferred over WAV when available
  - Retry-friendly: full text stored in job for re-submission

Routes:
  GET  /                          → frontend
  GET  /api/status
  POST /api/voices                → upload reference audio
  GET  /api/voices                → list voices
  PATCH /api/voices/{id}          → update ref_text
  DELETE /api/voices/{id}
  GET  /api/voices/{id}/audio     → stream reference WAV (for preview)
  GET  /api/voices/{id}/export    → .mimicry zip download
  POST /api/voices/import         → upload .mimicry zip
  POST /api/voices/mix            → blend two voices
  POST /api/synthesize            → enqueue job (202)
  GET  /api/jobs/{id}             → poll job
  POST /api/batch                 → enqueue batch (202)
  GET  /api/batch/{id}            → poll batch
  GET  /api/queue                 → all active + recent jobs
  POST /api/verify                → extract watermark from uploaded audio
  GET  /api/history               → last 40 records
  GET  /api/audio/{filename}      → serve audio
"""

import json
import logging
import os
import re as _re
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.voice_engine import OUTPUTS_DIR, VOICES_DIR, engine, extract_watermark

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")
logger = logging.getLogger(__name__)

FRONTEND_DIR      = Path(__file__).parent.parent / "frontend"
ALLOWED_AUDIO_EXT = {".wav", ".mp3", ".m4a", ".ogg", ".webm"}

# ── SQLite job persistence ─────────────────────────────────────────────────

_DB_PATH  = Path(__file__).parent / "storage" / "jobs.db"
_db_conn: Optional[sqlite3.Connection] = None
_db_lock  = threading.Lock()

_DDL = """
CREATE TABLE IF NOT EXISTS synth_job (
    id          TEXT PRIMARY KEY,
    status      TEXT,
    voice_id    TEXT,
    voice_name  TEXT,
    language    TEXT,
    speed       REAL,
    text_full   TEXT,
    text_preview TEXT,
    audio_url   TEXT,
    mp3_url     TEXT,
    filename    TEXT,
    watermark_id TEXT,
    error       TEXT,
    created_at  REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS batch_job (
    id          TEXT PRIMARY KEY,
    status      TEXT,
    voice_id    TEXT,
    total       INTEGER,
    completed   INTEGER,
    zip_url     TEXT,
    error       TEXT,
    created_at  REAL,
    finished_at REAL,
    items_json  TEXT
);
"""

def _db_init() -> None:
    global _db_conn
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db_conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    _db_conn.row_factory = sqlite3.Row
    for stmt in _DDL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            _db_conn.execute(stmt)
    _db_conn.commit()

def _db_save_synth(job: dict) -> None:
    if not _db_conn: return
    with _db_lock:
        _db_conn.execute("""
            INSERT OR REPLACE INTO synth_job
            (id,status,voice_id,voice_name,language,speed,text_full,text_preview,
             audio_url,mp3_url,filename,watermark_id,error,created_at,finished_at)
            VALUES (:id,:status,:voice_id,:voice_name,:language,:speed,:text_full,:text_preview,
                    :audio_url,:mp3_url,:filename,:watermark_id,:error,:created_at,:finished_at)
        """, {
            "id": job["id"], "status": job["status"],
            "voice_id": job.get("voice_id",""), "voice_name": job.get("voice_name",""),
            "language": job.get("language",""), "speed": job.get("speed",1.0),
            "text_full": job.get("text_full",""), "text_preview": job.get("text_preview",""),
            "audio_url": job.get("audio_url",""), "mp3_url": job.get("mp3_url",""),
            "filename": job.get("filename",""), "watermark_id": job.get("watermark_id",""),
            "error": job.get("error",""),
            "created_at": job.get("created_at",0), "finished_at": job.get("finished_at"),
        })
        _db_conn.commit()

def _db_save_batch(job: dict) -> None:
    if not _db_conn: return
    with _db_lock:
        _db_conn.execute("""
            INSERT OR REPLACE INTO batch_job
            (id,status,voice_id,total,completed,zip_url,error,created_at,finished_at,items_json)
            VALUES (:id,:status,:voice_id,:total,:completed,:zip_url,:error,
                    :created_at,:finished_at,:items_json)
        """, {
            "id": job["id"], "status": job["status"],
            "voice_id": job.get("voice_id",""),
            "total": job.get("total",0), "completed": job.get("completed",0),
            "zip_url": job.get("zip_url",""), "error": job.get("error",""),
            "created_at": job.get("created_at",0), "finished_at": job.get("finished_at"),
            "items_json": json.dumps(job.get("items",[])),
        })
        _db_conn.commit()

def _db_load_all(synth_store: dict, batch_store: dict) -> None:
    """Load persisted jobs into in-memory dicts on startup."""
    if not _db_conn: return
    for row in _db_conn.execute(
        "SELECT * FROM synth_job ORDER BY created_at DESC LIMIT 100"
    ).fetchall():
        d = dict(row)
        d["type"] = "synth"
        synth_store[d["id"]] = d
    for row in _db_conn.execute(
        "SELECT * FROM batch_job ORDER BY created_at DESC LIMIT 100"
    ).fetchall():
        d = dict(row)
        d["type"] = "batch"
        d["items"] = json.loads(d.pop("items_json", "[]"))
        batch_store[d["id"]] = d
    logger.info("Loaded %d synth + %d batch jobs from SQLite",
                len(synth_store), len(batch_store))

# ── Optional API key auth ──────────────────────────────────────────────────

_API_KEY = os.getenv("MIMICRY_API_KEY", "").strip()

async def _check_auth(request: Request) -> None:
    """Dependency: validates Bearer token if MIMICRY_API_KEY is set."""
    if not _API_KEY:
        return  # auth disabled — open access
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {_API_KEY}":
        raise HTTPException(
            status_code=401,
            detail="Unauthorized. Provide header: Authorization: Bearer <key>"
        )

# Write-guarded dependency (applied to POST/PATCH/DELETE routes)
_write_auth = [Depends(_check_auth)]

# ── Job stores ────────────────────────────────────────────────────────────

synth_jobs: dict[str, dict] = {}
batch_jobs: dict[str, dict] = {}
_executor  = ThreadPoolExecutor(max_workers=1)

MAX_QUEUE_HISTORY = 100


def _prune(store: dict) -> None:
    if len(store) > MAX_QUEUE_HISTORY:
        oldest = sorted(store, key=lambda k: store[k]["created_at"])
        for k in oldest[:len(store)-MAX_QUEUE_HISTORY]:
            del store[k]


def _make_synth_job(jid: str) -> dict:
    return {"id": jid, "type": "synth", "status": "pending",
            "audio_url": None, "mp3_url": None,
            "filename": None, "watermark_id": None,
            "voice_id": None, "voice_name": None,
            "language": None, "speed": 1.0,
            "text_full": None, "text_preview": None, "error": None,
            "created_at": time.time(), "finished_at": None}


def _make_batch_job(jid: str, total: int) -> dict:
    return {"id": jid, "type": "batch", "status": "pending",
            "voice_id": None,
            "total": total, "completed": 0,
            "items": [], "zip_url": None, "error": None,
            "created_at": time.time(), "finished_at": None}


# ── Background workers ────────────────────────────────────────────────────

def _run_synth(jid: str, text: str, voice_id: str, language: str, speed: float) -> None:
    job = synth_jobs[jid]
    job["status"] = "running"
    _db_save_synth(job)
    try:
        voice    = engine.get_voice(voice_id)
        out_path = engine.synthesize(text=text, voice_id=voice_id,
                                     language=language, speed=speed, wm_id=jid)
        mp3_path = out_path.with_suffix(".mp3")
        mp3_url  = f"/api/audio/{mp3_path.name}" if mp3_path.exists() else None
        audio_url = f"/api/audio/{out_path.name}"
        wm_id     = out_path.stem
        job.update(
            status="done", audio_url=audio_url, mp3_url=mp3_url,
            filename=out_path.name, watermark_id=wm_id,
            voice_name=voice["name"] if voice else voice_id,
            language=language, speed=speed,
            text_preview=text[:120], finished_at=time.time()
        )
        _db_save_synth(job)
        engine.record_history({
            "job_id": jid, "voice_name": job["voice_name"],
            "language": language, "speed": speed,
            "text_preview": job["text_preview"],
            "audio_url": audio_url, "mp3_url": mp3_url,
            "filename": out_path.name,
            "watermark_id": wm_id, "created_at": job["finished_at"],
        })
    except Exception as exc:
        logger.error("Synth job %s failed: %s", jid, exc)
        job.update(status="failed", error=str(exc), finished_at=time.time())
        _db_save_synth(job)


def _run_batch(bid: str, lines: list[str], voice_id: str,
               language: str, speed: float) -> None:
    job = batch_jobs[bid]
    job["status"] = "running"
    job["voice_id"] = voice_id
    _db_save_batch(job)

    voice = engine.get_voice(voice_id)
    vname = voice["name"] if voice else voice_id

    items = [{"index": i, "text": l, "status": "pending",
               "audio_url": None, "filename": None, "watermark_id": None}
             for i, l in enumerate(lines)]
    job["items"] = items

    for item in items:
        item["status"] = "running"
        try:
            out_path = engine.synthesize(item["text"], voice_id, language, speed)
            wm_id    = out_path.stem
            mp3_path = out_path.with_suffix(".mp3")
            item.update(
                status="done",
                audio_url=f"/api/audio/{(mp3_path.name if mp3_path.exists() else out_path.name)}",
                filename=out_path.name, watermark_id=wm_id
            )
            engine.record_history({
                "job_id": bid, "voice_name": vname, "language": language,
                "speed": speed, "text_preview": item["text"][:120],
                "audio_url": item["audio_url"], "filename": item["filename"],
                "watermark_id": wm_id, "created_at": time.time(),
            })
        except Exception as exc:
            logger.error("Batch %s item %d: %s", bid, item["index"], exc)
            item.update(status="failed", error=str(exc))
        job["completed"] += 1
        _db_save_batch(job)

    done_items = [it for it in items if it["status"] == "done"]
    if done_items:
        zip_path = OUTPUTS_DIR / f"batch_{bid}.zip"
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
            for it in done_items:
                fp = OUTPUTS_DIR / it["filename"]
                if fp.exists():
                    zf.write(str(fp), f"{it['index']:03d}_{it['filename']}")
        job["zip_url"] = f"/api/audio/batch_{bid}.zip"

    job.update(status="done", finished_at=time.time())
    _db_save_batch(job)


# ── Lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Mimicry v5 starting…")
    _db_init()
    _db_load_all(synth_jobs, batch_jobs)
    if _API_KEY:
        logger.info("API key auth enabled.")
    try:
        engine.load_model()
        logger.info("Models ready.")
    except Exception as exc:
        logger.error("Model load failed: %s", exc)
    yield
    _executor.shutdown(wait=False)
    logger.info("Mimicry shutting down.")


app = FastAPI(title="Mimicry API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/api/audio", StaticFiles(directory=str(OUTPUTS_DIR)), name="audio")


# ── Frontend ──────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def index(): return FileResponse(str(FRONTEND_DIR / "index.html"))

@app.get("/style.css", include_in_schema=False)
async def style(): return FileResponse(str(FRONTEND_DIR / "style.css"))

@app.get("/app.js", include_in_schema=False)
async def appjs(): return FileResponse(str(FRONTEND_DIR / "app.js"))


# ── Status ────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def status():
    return {"model_loaded": engine.is_loaded,
            "languages": engine.supported_languages(),
            "version": "1.0.0"}


# ── Voices ────────────────────────────────────────────────────────────────

@app.post("/api/voices", status_code=201, dependencies=_write_auth)
async def upload_voice(
    name:     str           = Form(...),
    audio:    UploadFile    = File(...),
    ref_text: Optional[str] = Form(None),
):
    suffix = Path(audio.filename or "").suffix.lower()
    if suffix not in ALLOWED_AUDIO_EXT:
        raise HTTPException(400, f"Unsupported format '{suffix}'.")
    if not name.strip():
        raise HTTPException(400, "Voice name cannot be empty.")
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        async with aiofiles.open(tmp_path, "wb") as f:
            await f.write(await audio.read())
        meta = engine.save_voice(name=name.strip(), src_audio=tmp_path,
                                 ref_text_override=ref_text)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    finally:
        if tmp_path.exists(): tmp_path.unlink()
    return meta


@app.get("/api/voices")
async def list_voices(): return engine.list_voices()


class UpdateVoiceBody(BaseModel):
    ref_text: str


@app.patch("/api/voices/{vid}", dependencies=_write_auth)
async def update_voice(vid: str, body: UpdateVoiceBody):
    meta = engine.update_voice_ref_text(vid, body.ref_text)
    if not meta: raise HTTPException(404, "Voice not found.")
    return meta


@app.delete("/api/voices/{vid}", dependencies=_write_auth)
async def delete_voice(vid: str):
    if not engine.delete_voice(vid): raise HTTPException(404, "Voice not found.")
    return {"deleted": vid}


@app.get("/api/voices/{vid}/audio")
async def get_voice_audio(vid: str):
    """Stream the reference WAV for voice preview."""
    voice = engine.get_voice(vid)
    if not voice: raise HTTPException(404, "Voice not found.")
    wav = Path(voice["wav"])
    if not wav.exists(): raise HTTPException(404, "Audio file missing.")
    return FileResponse(str(wav), media_type="audio/wav")


@app.get("/api/voices/{vid}/export")
async def export_voice(vid: str):
    zb = engine.export_voice(vid)
    if not zb: raise HTTPException(404, "Voice not found.")
    voice = engine.get_voice(vid)
    safe  = _re.sub(r'[^\w\-]', '_', voice["name"]) if voice else vid
    return Response(content=zb, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.mimicry"'})


@app.post("/api/voices/import", status_code=201, dependencies=_write_auth)
async def import_voice(file: UploadFile = File(...)):
    if not (file.filename or "").endswith((".mimicry", ".zip")):
        raise HTTPException(400, "Upload a .mimicry file.")
    try:
        meta = engine.import_voice(await file.read())
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return meta


class MixRequest(BaseModel):
    voice_id_a: str
    voice_id_b: str
    alpha:      float = Field(0.5, ge=0.0, le=1.0)
    name:       str   = Field(..., min_length=1, max_length=60)


@app.post("/api/voices/mix", status_code=201, dependencies=_write_auth)
async def mix_voices(req: MixRequest):
    try:
        meta = engine.mix_voices(req.voice_id_a, req.voice_id_b, req.alpha, req.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))
    return meta


# ── Synthesis ─────────────────────────────────────────────────────────────

class SynthRequest(BaseModel):
    text:     str   = Field(..., min_length=1, max_length=5000)
    voice_id: str
    language: str   = "en"
    speed:    float = Field(1.0, ge=0.5, le=2.0)


@app.post("/api/synthesize", status_code=202, dependencies=_write_auth)
async def synthesize(req: SynthRequest):
    if not engine.is_loaded:  raise HTTPException(503, "Model loading.")
    if not engine.get_voice(req.voice_id): raise HTTPException(404, "Voice not found.")
    if req.language not in engine.supported_languages():
        raise HTTPException(400, f"Unsupported language '{req.language}'.")
    jid = uuid.uuid4().hex[:12]
    job = _make_synth_job(jid)
    job["voice_id"]    = req.voice_id
    job["text_full"]   = req.text
    job["text_preview"] = req.text[:120]
    synth_jobs[jid] = job
    _db_save_synth(job)
    _executor.submit(_run_synth, jid, req.text, req.voice_id, req.language, req.speed)
    _prune(synth_jobs)
    return {"job_id": jid, "status": "pending"}


@app.get("/api/jobs/{jid}")
async def get_job(jid: str):
    j = synth_jobs.get(jid)
    if not j: raise HTTPException(404, "Job not found.")
    return j


# ── Batch ─────────────────────────────────────────────────────────────────

class BatchRequest(BaseModel):
    lines:    list[str] = Field(..., min_length=1, max_length=50)
    voice_id: str
    language: str   = "en"
    speed:    float = Field(1.0, ge=0.5, le=2.0)


@app.post("/api/batch", status_code=202, dependencies=_write_auth)
async def start_batch(req: BatchRequest):
    if not engine.is_loaded:  raise HTTPException(503, "Model loading.")
    if not engine.get_voice(req.voice_id): raise HTTPException(404, "Voice not found.")
    lines = [l.strip() for l in req.lines if l.strip()][:50]
    if not lines: raise HTTPException(400, "No non-empty lines.")
    bid = uuid.uuid4().hex[:12]
    batch_jobs[bid] = _make_batch_job(bid, len(lines))
    _db_save_batch(batch_jobs[bid])
    _executor.submit(_run_batch, bid, lines, req.voice_id, req.language, req.speed)
    _prune(batch_jobs)
    return {"batch_id": bid, "total": len(lines), "status": "pending"}


@app.get("/api/batch/{bid}")
async def get_batch(bid: str):
    j = batch_jobs.get(bid)
    if not j: raise HTTPException(404, "Batch not found.")
    return j


# ── Queue (unified view) ──────────────────────────────────────────────────

@app.get("/api/queue")
async def get_queue():
    """Return all synth + batch jobs sorted newest-first."""
    all_jobs = list(synth_jobs.values()) + list(batch_jobs.values())
    all_jobs.sort(key=lambda j: j["created_at"], reverse=True)
    return all_jobs[:50]


# ── Verify watermark ──────────────────────────────────────────────────────

@app.post("/api/verify")
async def verify_audio(audio: UploadFile = File(...)):
    """Extract the Mimicry watermark ID from an uploaded audio file."""
    suffix = Path(audio.filename or "tmp.wav").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        async with aiofiles.open(tmp_path, "wb") as f:
            await f.write(await audio.read())
        wm_id = extract_watermark(tmp_path)
    finally:
        if tmp_path.exists(): tmp_path.unlink()
    if wm_id:
        return {"watermark_found": True, "watermark_id": wm_id,
                "generated_by": "Mimicry"}
    return {"watermark_found": False, "watermark_id": None}


# ── History ───────────────────────────────────────────────────────────────

@app.get("/api/history")
async def get_history(): return engine.get_history()
