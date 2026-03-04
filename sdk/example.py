"""
Mimicry SDK — usage examples.

Run any example after starting the Mimicry server:
    python -m uvicorn backend.main:app --port 8000

Then:
    python sdk/example.py
"""

import sys
from pathlib import Path

# Allow running from the repo root without installing the SDK as a package
sys.path.insert(0, str(Path(__file__).parent.parent))

from sdk import Mimicry


def main() -> None:
    # ── Connect ────────────────────────────────────────────────────────────
    m = Mimicry("http://localhost:8000")

    print("Checking server status…")
    s = m.status()
    print(f"  model_loaded = {s['model_loaded']}")
    print(f"  version      = {s['version']}")
    print(f"  languages    = {s['languages']}")

    if not s["model_loaded"]:
        print("Waiting for model to load (this can take ~60s on first run)…")
        m.wait_for_model(timeout=300)

    # ── List voices ────────────────────────────────────────────────────────
    voices = m.list_voices()
    print(f"\nVoices saved: {len(voices)}")
    for v in voices:
        print(f"  [{v['id']}] {v['name']}  — ref_text: {v.get('ref_text','')[:60]}")

    if not voices:
        print("\nNo voices yet.  Upload one first via the web UI at http://localhost:8000")
        print("or call:  m.upload_voice('Alice', 'path/to/alice.wav')")
        return

    # ── Synthesize with the first available voice ──────────────────────────
    voice = voices[0]
    print(f"\nSynthesizing with voice '{voice['name']}'…")

    audio_bytes = m.synthesize(
        voice_id=voice["id"],
        text="Hello from the Mimicry SDK!  This is a zero-shot voice clone.",
        language="en",
        speed=1.0,
    )

    out_path = Path("sdk_output.wav")
    out_path.write_bytes(audio_bytes)
    print(f"  Saved → {out_path}  ({len(audio_bytes):,} bytes)")

    # ── Batch synthesis ────────────────────────────────────────────────────
    print("\nRunning batch synthesis (3 lines)…")
    items = m.batch(
        voice_id=voice["id"],
        lines=[
            "Line one: the quick brown fox.",
            "Line two: jumps over the lazy dog.",
            "Line three: Mimicry SDK makes this easy.",
        ],
    )
    for it in items:
        status = it["status"]
        text   = it["text"][:50]
        print(f"  [{it['index']}] {status:6s}  {text}")

    # ── Watermark verification ─────────────────────────────────────────────
    if out_path.exists():
        print("\nVerifying watermark in sdk_output.wav…")
        result = m.verify(out_path)
        if result["watermark_found"]:
            print(f"  Watermark found!  ID = {result['watermark_id']}")
        else:
            print("  No Mimicry watermark detected.")

    print("\nDone.")


if __name__ == "__main__":
    main()
