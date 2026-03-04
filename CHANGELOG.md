# Changelog

All notable changes to Mimicry are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [1.0.0] — 2026-03-04

### Added
- **Zero-shot voice cloning** using F5-TTS flow-matching model
  — clone any voice from 3–30 seconds of reference audio, no training required
- **FastAPI backend** (`backend/main.py`) with 18 REST endpoints:
  - Voice management: upload, list, delete, update ref-text, export, import, mix, preview
  - Synthesis: single-shot and batch jobs with async job queue
  - Audio serving: WAV and MP3 outputs
  - Watermark: embed + verify LSB watermarks in generated audio
  - History and queue inspection
- **Voice engine** (`backend/voice_engine.py`) featuring:
  - F5-TTS with automatic CPU/GPU detection
  - faster-whisper `small.en` for automatic reference transcription
  - Spectral noise reduction on uploaded reference audio (`noisereduce`)
  - EBU R128 loudness normalisation at −16 LUFS on every output (`pyloudnorm`)
  - MP3 output via `pydub` + ffmpeg alongside WAV
  - SSML-lite `<break time="Xs"/>` pause tags in synthesis text
  - Paragraph prosody (`\n\n` → 0.7 s pause)
  - α-blend voice mixing at waveform level
  - LSB audio watermarking (survives WAV copies)
- **React-style single-page frontend** (`frontend/`):
  - Dark modern UI with voice library, job queue table, history tab
  - Voice preview (play reference audio in-browser)
  - MP3 + WAV download buttons for every synthesis
  - Retry button on failed jobs
  - Keyboard shortcuts: `Ctrl+Enter` synthesise / `Escape` close modals
  - Full mobile responsive layout (780 px and 480 px breakpoints)
  - Optional API-key support via `?key=…` URL parameter
- **SQLite job persistence** — synth and batch jobs survive server restarts
- **Optional API key auth** — set `MIMICRY_API_KEY` env var to protect write endpoints
- **Python SDK** (`sdk/`) — synchronous (`Mimicry`) and async (`AsyncMimicry`) clients:
  - `upload_voice`, `synthesize`, `batch`, `mix`, `verify`, `export_voice`, `import_voice`
  - `download_batch_zip` convenience helper
  - `wait_for_model` cold-start helper
  - Distributed as `mimicry-sdk` on PyPI-compatible wheel
- **Docker support** — `Dockerfile` + `docker-compose.yml` with named HuggingFace cache volume
- **`start.bat`** — one-click Windows launcher
- **`setup_check.py`** — dependency verification script

### Technical stack
| Layer | Technology |
|-------|-----------|
| Voice model | F5-TTS (flow matching) |
| Transcription | faster-whisper small.en |
| Noise reduction | noisereduce 3 |
| Loudness | pyloudnorm (EBU R128) |
| API | FastAPI + uvicorn |
| Jobs | ThreadPoolExecutor + SQLite |
| Frontend | Vanilla JS + CSS |
| SDK sync | requests |
| SDK async | httpx + asyncio |
| Container | Docker / docker-compose |

---

## Prior development history (internal)

| Internal tag | Key milestone |
|---|---|
| v1 | XTTS v2 prototype, basic upload + synthesize |
| v2 | Job queue, batch synthesis, history endpoint |
| v3 | Voice mixing, watermarking, export/import |
| v4 | F5-TTS migration, frontend rebuild |
| v5 → 1.0.0 | GPU support, noise reduction, loudness norm, MP3 output, SQLite, async SDK |
