"""
Setup verification script.
Run with: python setup_check.py
"""

import sys

REQUIRED_PYTHON = (3, 9)
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):   print(f"  {GREEN}[OK]{RESET} {msg}")
def fail(msg): print(f"  {RED}[FAIL]{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}[WARN]{RESET} {msg}")
def head(msg): print(f"\n{BOLD}{msg}{RESET}")


head("1. Python version")
major, minor = sys.version_info[:2]
if (major, minor) >= REQUIRED_PYTHON:
    ok(f"Python {major}.{minor} (>= {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]})")
else:
    fail(f"Python {major}.{minor} — need >= {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}")
    sys.exit(1)


head("2. Required packages")
packages = [
    ("fastapi",          "fastapi"),
    ("uvicorn",          "uvicorn"),
    ("multipart",        "python_multipart"),
    ("pydub",            "pydub"),
    ("soundfile",        "soundfile"),
    ("numpy",            "numpy"),
    ("torch",            "torch"),
    ("aiofiles",         "aiofiles"),
    ("TTS (Coqui)",      "TTS"),
]

missing = []
for label, module in packages:
    try:
        __import__(module)
        ok(label)
    except ImportError:
        fail(f"{label}  << NOT installed")
        missing.append(module)

if missing:
    print(f"\n{YELLOW}Install missing packages:{RESET}")
    print(f"  pip install {' '.join(missing)}")
    print(f"\n  Or install everything at once:")
    print(f"  pip install -r requirements.txt")
else:
    head("3. Torch device")
    import torch
    device = "CUDA (GPU)" if torch.cuda.is_available() else "CPU"
    if torch.cuda.is_available():
        ok(f"Device: {device} — {torch.cuda.get_device_name(0)}")
    else:
        warn(f"Device: CPU (no CUDA GPU detected — inference will be slower)")

    head("4. Storage directories")
    from pathlib import Path
    base = Path(__file__).parent / "backend" / "storage"
    for d in ("voices", "embeddings", "outputs"):
        p = base / d
        p.mkdir(parents=True, exist_ok=True)
        ok(f"backend/storage/{d}/")

    print(f"\n{GREEN}{BOLD}All checks passed!{RESET}")
    print(f"\nStart the server:")
    print(f"  Windows: double-click {BOLD}start.bat{RESET}")
    print(f"  Terminal: {BOLD}python -m uvicorn backend.main:app --port 8000{RESET}")
    print(f"\nThen open: {BOLD}http://localhost:8000{RESET}\n")
