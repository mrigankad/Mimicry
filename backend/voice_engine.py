"""
Mimicry — Voice Engine v5
F5-TTS zero-shot voice cloning.

New in v5:
  - GPU auto-detection (CUDA → CPU fallback)
  - Whisper upgraded to small.en (better transcription)
  - Audio denoising on reference upload (noisereduce, optional)
  - Loudness normalization on every output (pyloudnorm EBU R128 −16 LUFS, optional)
  - MP3 output alongside WAV for smaller downloads
  - SSML-lite: <break time="Xs"/> silence tags
  - Paragraph-aware prosody: double-newline → 700 ms pause
"""

import io
import json
import logging
import re
import uuid
import zipfile
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
import torch

logger = logging.getLogger(__name__)

_BASE          = Path(__file__).parent / "storage"
VOICES_DIR     = _BASE / "voices"
EMBEDDINGS_DIR = _BASE / "embeddings"
OUTPUTS_DIR    = _BASE / "outputs"
HISTORY_FILE   = OUTPUTS_DIR / "history.json"

for _d in (VOICES_DIR, EMBEDDINGS_DIR, OUTPUTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Auto-detect device
_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
logger.info("Voice engine device: %s", _DEVICE)

EMOTION_SPEED = {
    "fast": 1.35, "slow": 0.70, "whisper": 0.80,
    "excited": 1.20, "calm": 0.85,
}

# ── audio helpers ─────────────────────────────────────────────────────────

def _convert_to_wav(src: Path, dst: Path) -> None:
    from pydub import AudioSegment
    ext = src.suffix.lower().lstrip(".")
    fmt = {"mp3":"mp3","wav":"wav","m4a":"mp4","ogg":"ogg","webm":"webm"}.get(ext, ext)
    audio = AudioSegment.from_file(str(src), format=fmt)
    audio = audio.set_channels(1).set_frame_rate(24000)
    audio.export(str(dst), format="wav")

def _wav_to_mp3(wav_path: Path, mp3_path: Path, bitrate: str = "192k") -> bool:
    """Convert WAV to MP3. Returns True on success, False if ffmpeg is unavailable."""
    try:
        from pydub import AudioSegment
        seg = AudioSegment.from_wav(str(wav_path))
        seg.export(str(mp3_path), format="mp3", bitrate=bitrate)
        return True
    except Exception as exc:
        logger.debug("MP3 export skipped: %s", exc)
        return False

def _silence(sr: int, secs: float = 0.28) -> np.ndarray:
    return np.zeros(int(sr * secs), dtype=np.float32)

def _trim_silence(wav: np.ndarray, sr: int, pad_ms: int = 60) -> np.ndarray:
    thr = 10 ** (-38.0 / 20.0)
    idx = np.where(np.abs(wav) > thr)[0]
    if not len(idx): return wav
    pad = int(sr * pad_ms / 1000)
    return wav[max(0, idx[0]-pad) : min(len(wav), idx[-1]+pad)]

def _normalize_peak(wav: np.ndarray) -> np.ndarray:
    peak = np.max(np.abs(wav))
    return wav / (peak + 1e-8) if peak > 0 else wav

def _denoise_audio(wav: np.ndarray, sr: int) -> np.ndarray:
    """Apply spectral noise reduction. Gracefully skipped if noisereduce not installed."""
    if len(wav) / sr < 2.0:
        return wav   # too short to estimate noise profile
    try:
        import noisereduce as nr
        reduced = nr.reduce_noise(y=wav, sr=sr, prop_decrease=0.70, stationary=False)
        return reduced.astype(np.float32)
    except Exception as exc:
        logger.debug("Denoising skipped: %s", exc)
        return wav

def _normalize_loudness(wav: np.ndarray, sr: int, target_lufs: float = -16.0) -> np.ndarray:
    """EBU R128 integrated loudness normalization. Gracefully skipped if pyloudnorm not installed."""
    if len(wav) / sr < 0.5:
        return wav  # too short for BS.1770 loudness meter
    try:
        import pyloudnorm as pyln
        meter    = pyln.Meter(sr)
        loudness = meter.integrated_loudness(wav.astype(np.float64))
        if np.isinf(loudness) or np.isnan(loudness):
            return wav  # silence / degenerate audio
        normalized = pyln.normalize.loudness(wav.astype(np.float64), loudness, target_lufs)
        return np.clip(normalized, -1.0, 1.0).astype(np.float32)
    except Exception as exc:
        logger.debug("Loudness normalization skipped: %s", exc)
        return wav

# ── watermark ─────────────────────────────────────────────────────────────

_WM_PREFIX = "MIMICRY:"

def _embed_watermark(wav: np.ndarray, wm_id: str) -> np.ndarray:
    """
    Embed wm_id invisibly in the LSB of the first N audio samples.
    Inaudible: changes each sample by at most 1/32767 ≈ 0.003%.
    NOTE: survives WAV storage but not MP3 compression.
    """
    message = _WM_PREFIX + wm_id + "\x00"
    bits    = "".join(format(ord(c), "08b") for c in message)
    if len(bits) > len(wav):
        return wav  # clip too short — skip
    i16 = np.clip(wav * 32767.0, -32767, 32767).astype(np.int16)
    for i, b in enumerate(bits):
        i16[i] = (int(i16[i]) & 0xFFFE) | int(b)
    return i16.astype(np.float32) / 32767.0

def extract_watermark(audio_path: Path) -> str:
    """Return embedded Mimicry ID, or empty string if none found."""
    try:
        path = audio_path
        # For MP3, decode to WAV first
        if audio_path.suffix.lower() in (".mp3", ".m4a", ".ogg"):
            from pydub import AudioSegment
            import io as _io
            seg  = AudioSegment.from_file(str(audio_path))
            seg  = seg.set_channels(1).set_frame_rate(24000)
            buf  = _io.BytesIO()
            seg.export(buf, format="wav")
            buf.seek(0)
            wav, _ = sf.read(buf)
        else:
            wav, _ = sf.read(str(path))

        if wav.ndim > 1:
            wav = wav[:, 0]
        i16  = np.clip(wav * 32767.0, -32767, 32767).astype(np.int16)
        bits = [str(int(i16[i]) & 1) for i in range(min(1024, len(i16)))]
        chars: list[str] = []
        for i in range(0, len(bits) - 7, 8):
            byte = int("".join(bits[i:i+8]), 2)
            if byte == 0: break
            chars.append(chr(byte))
        text = "".join(chars)
        return text[len(_WM_PREFIX):] if text.startswith(_WM_PREFIX) else ""
    except Exception:
        return ""

# ── text / SSML helpers ───────────────────────────────────────────────────

_TAG_RE   = re.compile(r'\[(\w+)\](.*?)\[/\1\]', re.DOTALL)
_BREAK_RE = re.compile(r'<break\s+time="([0-9.]+)s"\s*/?>', re.IGNORECASE)

def _parse_segments(text: str) -> list[dict]:
    """
    Parse emotion tags + SSML <break> tags into a flat segment list.

    Returns list of:
      {"kind": "speak", "text": str, "speed": float}
    | {"kind": "pause", "secs": float}
    """
    result: list[dict] = []

    # Split on <break> tags first; pieces alternate text / duration
    pieces = _BREAK_RE.split(text)
    for idx, piece in enumerate(pieces):
        if idx % 2 == 1:
            # Captured group → pause duration
            result.append({"kind": "pause", "secs": max(0.05, float(piece))})
        else:
            if not piece.strip():
                continue
            # Parse emotion tags within this text piece
            last = 0
            for m in _TAG_RE.finditer(piece):
                before = piece[last:m.start()].strip()
                if before:
                    result.append({"kind": "speak", "text": before, "speed": 1.0})
                spd = EMOTION_SPEED.get(m.group(1).lower(), 1.0)
                result.append({"kind": "speak", "text": m.group(2).strip(), "speed": spd})
                last = m.end()
            tail = piece[last:].strip()
            if tail:
                result.append({"kind": "speak", "text": tail, "speed": 1.0})

    return result or [{"kind": "speak", "text": text.strip(), "speed": 1.0}]

def _chunk_text(text: str, max_chars: int = 180) -> list[str]:
    text = " ".join(text.split())
    raw  = re.split(r'(?<=[.!?…])\s+', text)
    chunks: list[str] = []
    for sent in raw:
        if not sent.strip(): continue
        if len(sent) <= max_chars:
            chunks.append(sent.strip())
        else:
            parts = re.split(r'(?<=[,;–—])\s+', sent)
            buf = ""
            for p in parts:
                if len(buf) + len(p) + 1 <= max_chars:
                    buf = (buf + " " + p).strip()
                else:
                    if buf: chunks.append(buf)
                    buf = p.strip()
            if buf: chunks.append(buf)
    return [c for c in chunks if c.strip()]

# ── VoiceEngine ───────────────────────────────────────────────────────────

class VoiceEngine:
    def __init__(self):
        self._tts = None
        self._asr = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def load_model(self) -> None:
        if self._tts is not None: return
        logger.info("Loading Whisper small.en (device=cpu)…")
        try:
            from faster_whisper import WhisperModel
            self._asr = WhisperModel("small.en", device="cpu", compute_type="int8")
            logger.info("Whisper ready.")
        except Exception as exc:
            logger.warning("Whisper unavailable: %s", exc)
        logger.info("Loading F5-TTS (device=%s)…", _DEVICE)
        from f5_tts.api import F5TTS
        self._tts = F5TTS(device=_DEVICE)
        logger.info("F5-TTS ready.")

    @property
    def is_loaded(self) -> bool:
        return self._tts is not None

    # ── validation ────────────────────────────────────────────────────────

    def validate_audio(self, wav_path: Path) -> list[str]:
        warns: list[str] = []
        try:
            data, sr = sf.read(str(wav_path))
            dur = len(data) / sr
            if dur < 3.0:   warns.append(f"Clip is short ({dur:.1f}s). Use 5–15s for best quality.")
            if dur > 90.0:  warns.append(f"Clip is long ({dur:.0f}s). 30s is usually enough.")
            rms = float(np.sqrt(np.mean(data ** 2)))
            if rms < 0.005: warns.append("Audio level is very low — may be silent or too quiet.")
            if rms > 0.95:  warns.append("Audio may be clipping — lower the recording volume.")
            if sr < 16000:  warns.append(f"Low sample rate ({sr} Hz). Prefer 22 kHz or higher.")
        except Exception as exc:
            warns.append(f"Could not analyse audio: {exc}")
        return warns

    # ── voice profiles ────────────────────────────────────────────────────

    def save_voice(self, name: str, src_audio: Path,
                   ref_text_override: Optional[str] = None) -> dict:
        vid  = str(uuid.uuid4())[:8]
        wav  = VOICES_DIR / f"{vid}.wav"
        meta = VOICES_DIR / f"{vid}.json"

        _convert_to_wav(src_audio, wav)

        # Apply denoising (optional, best-effort)
        try:
            data, sr = sf.read(str(wav))
            data = _denoise_audio(data.astype(np.float32), sr)
            sf.write(str(wav), data, sr)
        except Exception as exc:
            logger.debug("Denoising skipped during save: %s", exc)

        data, sr = sf.read(str(wav))
        dur    = round(len(data) / sr, 2)
        warns  = self.validate_audio(wav)
        ref_text = (ref_text_override.strip()
                    if ref_text_override and ref_text_override.strip()
                    else self._transcribe(wav))
        m = {"id": vid, "name": name, "wav": str(wav), "duration": dur,
             "ref_text": ref_text, "warnings": warns,
             "audio_url": f"/api/voices/{vid}/audio"}
        meta.write_text(json.dumps(m, indent=2))
        logger.info("Saved voice %s (%s)", vid, name)
        return m

    def update_voice_ref_text(self, vid: str, ref_text: str) -> Optional[dict]:
        p = VOICES_DIR / f"{vid}.json"
        if not p.exists(): return None
        m = json.loads(p.read_text())
        m["ref_text"] = ref_text.strip()
        p.write_text(json.dumps(m, indent=2))
        m["audio_url"] = f"/api/voices/{vid}/audio"
        return m

    def list_voices(self) -> list[dict]:
        out = []
        for f in sorted(VOICES_DIR.glob("*.json")):
            try:
                v = json.loads(f.read_text())
                v["audio_url"] = f"/api/voices/{v['id']}/audio"
                out.append(v)
            except Exception:
                pass
        return out

    def get_voice(self, vid: str) -> Optional[dict]:
        p = VOICES_DIR / f"{vid}.json"
        if not p.exists(): return None
        v = json.loads(p.read_text())
        v["audio_url"] = f"/api/voices/{vid}/audio"
        return v

    def delete_voice(self, vid: str) -> bool:
        deleted = False
        for ext in (".wav", ".json"):
            p = VOICES_DIR / f"{vid}{ext}"
            if p.exists(): p.unlink(); deleted = True
        return deleted

    # ── voice mixing ──────────────────────────────────────────────────────

    def mix_voices(self, vid_a: str, vid_b: str, alpha: float, name: str) -> dict:
        """Blend two reference audios: result = α·A + (1-α)·B."""
        va = self.get_voice(vid_a)
        vb = self.get_voice(vid_b)
        if not va or not vb:
            raise ValueError("Both source voices must exist.")

        wav_a, sr_a = sf.read(va["wav"])
        wav_b, sr_b = sf.read(vb["wav"])
        wav_a = _normalize_peak(wav_a.astype(np.float32))
        wav_b = _normalize_peak(wav_b.astype(np.float32))

        n     = min(len(wav_a), len(wav_b))
        mixed = _normalize_peak(alpha * wav_a[:n] + (1.0 - alpha) * wav_b[:n])

        vid       = str(uuid.uuid4())[:8]
        wav_path  = VOICES_DIR / f"{vid}.wav"
        meta_path = VOICES_DIR / f"{vid}.json"
        sf.write(str(wav_path), mixed, sr_a)

        ref_text = va.get("ref_text") or vb.get("ref_text") or ""
        m = {
            "id": vid, "name": name, "wav": str(wav_path),
            "duration": round(n / sr_a, 2), "ref_text": ref_text,
            "warnings": [], "is_mix": True,
            "mix_alpha": round(alpha, 2),
            "mix_source_a": {"id": vid_a, "name": va["name"]},
            "mix_source_b": {"id": vid_b, "name": vb["name"]},
            "audio_url": f"/api/voices/{vid}/audio",
        }
        meta_path.write_text(json.dumps(m, indent=2))
        logger.info("Mixed voice %s: %.0f%% %s + %.0f%% %s",
                    vid, alpha*100, va["name"], (1-alpha)*100, vb["name"])
        return m

    # ── export / import ───────────────────────────────────────────────────

    def export_voice(self, vid: str) -> Optional[bytes]:
        voice = self.get_voice(vid)
        if not voice: return None
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(voice["wav"], "reference.wav")
            zf.writestr("meta.json", json.dumps(
                {k: voice[k] for k in ("name","ref_text","duration","is_mix",
                                        "mix_alpha","mix_source_a","mix_source_b")
                 if k in voice}, indent=2))
        return buf.getvalue()

    def import_voice(self, zip_bytes: bytes) -> dict:
        buf = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(buf) as zf:
            if "reference.wav" not in zf.namelist() or "meta.json" not in zf.namelist():
                raise ValueError("Invalid .mimicry file.")
            raw       = json.loads(zf.read("meta.json"))
            wav_bytes = zf.read("reference.wav")
        vid       = str(uuid.uuid4())[:8]
        wav       = VOICES_DIR / f"{vid}.wav"
        meta_path = VOICES_DIR / f"{vid}.json"
        wav.write_bytes(wav_bytes)
        data, sr = sf.read(str(wav))
        m = {"id": vid, "name": raw.get("name","Imported"), "wav": str(wav),
             "duration": round(len(data)/sr, 2), "ref_text": raw.get("ref_text",""),
             "warnings": [], "audio_url": f"/api/voices/{vid}/audio"}
        if raw.get("is_mix"):
            m.update(is_mix=True, mix_alpha=raw.get("mix_alpha"),
                     mix_source_a=raw.get("mix_source_a"),
                     mix_source_b=raw.get("mix_source_b"))
        meta_path.write_text(json.dumps(m, indent=2))
        return m

    # ── transcription ─────────────────────────────────────────────────────

    def _transcribe(self, wav_path: Path) -> str:
        if not self._asr: return ""
        try:
            segs, _ = self._asr.transcribe(str(wav_path), beam_size=3)
            return " ".join(s.text.strip() for s in segs).strip()
        except Exception as exc:
            logger.warning("Transcription failed: %s", exc); return ""

    # ── synthesis ─────────────────────────────────────────────────────────

    def _synth_chunk(self, chunk: str, wav_path: Path,
                     ref_text: str, speed: float) -> tuple[np.ndarray, int]:
        wav_arr, sr, _ = self._tts.infer(
            ref_file=str(wav_path), ref_text=ref_text, gen_text=chunk,
            seed=42, remove_silence=True, speed=speed)
        return _trim_silence(wav_arr.astype(np.float32), sr), sr

    def synthesize(self, text: str, voice_id: str, language: str = "en",
                   speed: float = 1.0, wm_id: Optional[str] = None) -> Path:
        """
        Synthesize speech. Returns the WAV path.
        Also creates a sibling .mp3 file if ffmpeg is available.
        """
        if not self.is_loaded: raise RuntimeError("Model not loaded.")
        voice = self.get_voice(voice_id)
        if not voice: raise ValueError(f"Voice '{voice_id}' not found.")

        ref_text = voice.get("ref_text", "")
        wav_path = Path(voice["wav"])
        out_id   = wm_id or uuid.uuid4().hex[:12]
        out_path = OUTPUTS_DIR / f"{out_id}.wav"

        # Normalize paragraph breaks → SSML pause, collapse single newlines
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = re.sub(r'\n{2,}', ' <break time="0.7s"/> ', text)
        text = text.replace('\n', ' ')

        segments = _parse_segments(text)
        parts: list[np.ndarray] = []
        sr    = 24000

        # Count speak segments for progress logging
        speak_total = sum(1 for s in segments if s["kind"] == "speak")
        speak_done  = 0

        for seg_idx, seg in enumerate(segments):
            if seg["kind"] == "pause":
                parts.append(_silence(sr, seg["secs"]))
                continue

            eff    = round(max(0.5, min(2.0, speed * seg["speed"])), 2)
            chunks = _chunk_text(seg["text"])
            for ci, chunk in enumerate(chunks):
                speak_done += 1
                logger.info("  Chunk %d/%d (spd=%.2f): %r",
                            speak_done, speak_total, eff, chunk[:50])
                wav_arr, sr = self._synth_chunk(chunk, wav_path, ref_text, eff)
                parts.append(wav_arr)
                # Inter-chunk silence (within same segment)
                if ci < len(chunks) - 1:
                    parts.append(_silence(sr, 0.28))

            # Inter-segment silence (between emotion/text segments, not after pause)
            next_seg = segments[seg_idx + 1] if seg_idx + 1 < len(segments) else None
            if next_seg and next_seg["kind"] != "pause":
                parts.append(_silence(sr, 0.22))

        combined = np.concatenate(parts) if parts else np.zeros(sr, dtype=np.float32)

        # Post-process: loudness normalize then peak normalize
        combined = _normalize_loudness(combined, sr)
        combined = np.clip(combined, -1.0, 1.0)

        # Embed watermark (LSB, WAV-only)
        combined = _embed_watermark(combined, out_id)

        sf.write(str(out_path), combined, sr)
        logger.info("WAV done -> %s", out_path.name)

        # Best-effort MP3 export (requires ffmpeg)
        _wav_to_mp3(out_path, out_path.with_suffix(".mp3"))

        return out_path

    # ── history ───────────────────────────────────────────────────────────

    def record_history(self, entry: dict) -> None:
        hist: list[dict] = []
        if HISTORY_FILE.exists():
            try: hist = json.loads(HISTORY_FILE.read_text())
            except Exception: pass
        hist.insert(0, entry)
        HISTORY_FILE.write_text(json.dumps(hist[:40], indent=2))

    def get_history(self) -> list[dict]:
        if not HISTORY_FILE.exists(): return []
        try: return json.loads(HISTORY_FILE.read_text())
        except Exception: return []

    def supported_languages(self) -> list[str]:
        return ["en", "zh-cn"]


engine = VoiceEngine()
