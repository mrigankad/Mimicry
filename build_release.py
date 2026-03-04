"""
build_release.py
================
Produces dist/mimicry-v1.0.0.zip — a deployable source archive containing
everything needed to run Mimicry (backend, frontend, SDK, docs, scripts).

Usage:
    python build_release.py

Output:
    dist/
        mimicry-v1.0.0.zip          <- source archive (run anywhere)
        mimicry_sdk-1.0.0-py3-none-any.whl  <- installed by pip build
"""

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

VERSION = "1.0.0"
ROOT    = Path(__file__).parent.resolve()
DIST    = ROOT / "dist"

# Files and directories to include in the ZIP archive
INCLUDE = [
    "backend/main.py",
    "backend/voice_engine.py",
    "frontend/index.html",
    "frontend/style.css",
    "frontend/app.js",
    "sdk/__init__.py",
    "sdk/client.py",
    "sdk/async_client.py",
    "sdk/example.py",
    "sdk/py.typed",
    "requirements.txt",
    "pyproject.toml",
    "start.bat",
    "setup_check.py",
    "README.md",
    "CHANGELOG.md",
    ".gitignore",
    "Dockerfile",
    "docker-compose.yml",
    ".dockerignore",
]


def banner(msg: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


def run(cmd: list[str], **kwargs) -> None:
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        print(f"  ERROR: command exited with code {result.returncode}")
        sys.exit(result.returncode)


def build_zip() -> Path:
    zip_name = f"mimicry-v{VERSION}.zip"
    zip_path = DIST / zip_name
    missing   = []

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in INCLUDE:
            src = ROOT / rel
            if not src.exists():
                missing.append(rel)
                print(f"  [skip] {rel}  (not found)")
                continue
            arcname = f"mimicry-v{VERSION}/{rel}"
            zf.write(src, arcname)
            print(f"  [add]  {rel}")

    if missing:
        print(f"\n  Warning: {len(missing)} file(s) were not found and were skipped.")

    return zip_path


def build_wheel() -> None:
    """Run `python -m build` to create the SDK wheel."""
    try:
        import build  # noqa: F401 — check it's installed
    except ImportError:
        print("  'build' package not found — skipping wheel build.")
        print("  Install with: pip install build")
        return

    run([sys.executable, "-m", "build", "--wheel", "--outdir", str(DIST)], cwd=ROOT)


def main() -> None:
    banner(f"Mimicry v{VERSION} — release builder")

    # Create dist/
    DIST.mkdir(exist_ok=True)
    print(f"\n  Output directory: {DIST}")

    # 1. Build source ZIP
    banner("Step 1 / 2 — Building source archive")
    zip_path = build_zip()
    size_kb   = zip_path.stat().st_size // 1024
    print(f"\n  Created: {zip_path.name}  ({size_kb} KB)")

    # 2. Build SDK wheel
    banner("Step 2 / 2 — Building SDK wheel (mimicry-sdk)")
    build_wheel()

    # Summary
    banner("Release artifacts")
    for f in sorted(DIST.iterdir()):
        size = f.stat().st_size
        unit = "KB" if size < 1_000_000 else "MB"
        val  = size // 1024 if size < 1_000_000 else size // (1024 * 1024)
        print(f"  {f.name:<50}  {val:>5} {unit}")

    print(f"\n  Done! Artifacts are in: {DIST}\n")


if __name__ == "__main__":
    main()
