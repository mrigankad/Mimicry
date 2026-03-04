"""
Mimicry Python SDK
==================
A lightweight, synchronous client for the Mimicry voice-cloning API.

Quickstart
----------
    from sdk import Mimicry

    m = Mimicry("http://localhost:8000")

    # Upload a reference voice
    voice = m.upload_voice("Alice", "/path/to/alice.wav")

    # Synthesize speech (blocks until ready)
    audio_bytes = m.synthesize(voice["id"], "Hello from Mimicry!")
    with open("output.wav", "wb") as f:
        f.write(audio_bytes)

    # Batch synthesis
    results = m.batch(voice["id"], ["Line one.", "Line two.", "Line three."])
    for r in results:
        print(r["text"], "→", r["audio_url"])

    # Blend two voices
    mixed = m.mix("alice_id", "bob_id", alpha=0.3, name="AliceBob")
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any, BinaryIO, Union

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Mimicry SDK requires 'requests'. Install it with: pip install requests"
    ) from exc


# ── Exceptions ────────────────────────────────────────────────────────────────

class MimicryError(Exception):
    """Raised when the Mimicry API returns an error response."""
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


class JobTimeoutError(TimeoutError):
    """Raised when a synthesis job does not finish within the timeout."""


# ── Client ────────────────────────────────────────────────────────────────────

class Mimicry:
    """Synchronous Mimicry API client.

    Parameters
    ----------
    base_url:
        Root URL of the Mimicry server, e.g. ``"http://localhost:8000"``.
    timeout:
        HTTP request timeout in seconds (default 60).
    poll_interval:
        Seconds between job status polls (default 2.5).
    job_timeout:
        Maximum seconds to wait for a synthesis job to finish (default 300).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
        poll_interval: float = 2.5,
        job_timeout: float = 300.0,
    ) -> None:
        self.base_url      = base_url.rstrip("/")
        self.timeout       = timeout
        self.poll_interval = poll_interval
        self.job_timeout   = job_timeout
        self._session      = requests.Session()
        if api_key:
            self._session.headers["Authorization"] = f"Bearer {api_key}"

    # ── Internals ─────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _check(self, resp: requests.Response) -> dict:
        if not resp.ok:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise MimicryError(resp.status_code, detail)
        return resp.json()

    def _poll_job(self, job_id: str) -> dict:
        """Block until the synth job is done or failed; return job dict."""
        deadline = time.monotonic() + self.job_timeout
        while True:
            resp = self._session.get(self._url(f"/api/jobs/{job_id}"),
                                     timeout=self.timeout)
            job  = self._check(resp)
            if job["status"] == "done":
                return job
            if job["status"] == "failed":
                raise MimicryError(500, f"Job {job_id} failed: {job.get('error')}")
            if time.monotonic() > deadline:
                raise JobTimeoutError(
                    f"Job {job_id} did not finish within {self.job_timeout}s"
                )
            time.sleep(self.poll_interval)

    def _poll_batch(self, batch_id: str) -> dict:
        """Block until the batch job is done; return batch dict."""
        deadline = time.monotonic() + self.job_timeout
        while True:
            resp  = self._session.get(self._url(f"/api/batch/{batch_id}"),
                                      timeout=self.timeout)
            batch = self._check(resp)
            if batch["status"] == "done":
                return batch
            if batch["status"] == "failed":
                raise MimicryError(500, f"Batch {batch_id} failed: {batch.get('error')}")
            if time.monotonic() > deadline:
                raise JobTimeoutError(
                    f"Batch {batch_id} did not finish within {self.job_timeout}s"
                )
            time.sleep(self.poll_interval)

    def _download_audio(self, audio_url: str) -> bytes:
        """Download audio bytes given a server-relative URL."""
        url  = self._url(audio_url) if audio_url.startswith("/") else audio_url
        resp = self._session.get(url, timeout=self.timeout)
        if not resp.ok:
            raise MimicryError(resp.status_code, f"Audio download failed: {resp.text}")
        return resp.content

    # ── Status ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return server status: model_loaded, languages, version."""
        return self._check(
            self._session.get(self._url("/api/status"), timeout=self.timeout)
        )

    def wait_for_model(self, timeout: float = 120.0, interval: float = 3.0) -> None:
        """Block until the model is loaded (useful after a cold start)."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                s = self.status()
                if s.get("model_loaded"):
                    return
            except (requests.ConnectionError, MimicryError):
                pass
            if time.monotonic() > deadline:
                raise JobTimeoutError(f"Model did not load within {timeout}s")
            time.sleep(interval)

    # ── Voices ────────────────────────────────────────────────────────────

    def list_voices(self) -> list[dict]:
        """Return all saved voice profiles."""
        return self._check(
            self._session.get(self._url("/api/voices"), timeout=self.timeout)
        )

    def upload_voice(
        self,
        name: str,
        audio: Union[str, Path, bytes, BinaryIO],
        ref_text: str | None = None,
        filename: str = "reference.wav",
    ) -> dict:
        """Upload a reference audio clip and create a voice profile.

        Parameters
        ----------
        name:
            Display name for the voice (e.g. ``"Alice"``).
        audio:
            Path to an audio file, raw bytes, or a file-like object.
        ref_text:
            Optional transcript of the audio. Leave as ``None`` to let
            Whisper auto-transcribe.
        filename:
            Filename hint for the upload (affects MIME detection).

        Returns
        -------
        dict
            The saved voice metadata (id, name, ref_text, duration, …).
        """
        if isinstance(audio, (str, Path)):
            with open(audio, "rb") as f:
                data = f.read()
            filename = Path(audio).name
        elif isinstance(audio, bytes):
            data = audio
        else:
            data = audio.read()

        files  = {"audio": (filename, io.BytesIO(data))}
        fields = {"name": name}
        if ref_text is not None:
            fields["ref_text"] = ref_text

        resp = self._session.post(
            self._url("/api/voices"), data=fields, files=files, timeout=self.timeout
        )
        return self._check(resp)

    def delete_voice(self, voice_id: str) -> dict:
        """Delete a voice profile by ID."""
        return self._check(
            self._session.delete(self._url(f"/api/voices/{voice_id}"),
                                 timeout=self.timeout)
        )

    def update_ref_text(self, voice_id: str, ref_text: str) -> dict:
        """Update the reference transcript for a voice."""
        return self._check(
            self._session.patch(
                self._url(f"/api/voices/{voice_id}"),
                json={"ref_text": ref_text},
                timeout=self.timeout,
            )
        )

    def export_voice(self, voice_id: str) -> bytes:
        """Download a voice profile as a `.mimicry` zip archive."""
        resp = self._session.get(
            self._url(f"/api/voices/{voice_id}/export"), timeout=self.timeout
        )
        if not resp.ok:
            raise MimicryError(resp.status_code, resp.text)
        return resp.content

    def import_voice(
        self,
        data: Union[str, Path, bytes, BinaryIO],
        filename: str = "voice.mimicry",
    ) -> dict:
        """Import a voice profile from a `.mimicry` archive."""
        if isinstance(data, (str, Path)):
            with open(data, "rb") as f:
                raw = f.read()
            filename = Path(data).name
        elif isinstance(data, bytes):
            raw = data
        else:
            raw = data.read()

        resp = self._session.post(
            self._url("/api/voices/import"),
            files={"file": (filename, io.BytesIO(raw))},
            timeout=self.timeout,
        )
        return self._check(resp)

    def mix(
        self,
        voice_id_a: str,
        voice_id_b: str,
        name: str,
        alpha: float = 0.5,
    ) -> dict:
        """Blend two voice profiles into a new hybrid voice.

        Parameters
        ----------
        voice_id_a, voice_id_b:
            IDs of the two source voices.
        name:
            Name for the new mixed voice.
        alpha:
            Weight toward voice A (0.0 = all B, 1.0 = all A). Default 0.5.

        Returns
        -------
        dict
            The new voice profile metadata.
        """
        resp = self._session.post(
            self._url("/api/voices/mix"),
            json={"voice_id_a": voice_id_a, "voice_id_b": voice_id_b,
                  "alpha": alpha, "name": name},
            timeout=self.timeout,
        )
        return self._check(resp)

    # ── Synthesis ─────────────────────────────────────────────────────────

    def synthesize(
        self,
        voice_id: str,
        text: str,
        language: str = "en",
        speed: float = 1.0,
        *,
        return_bytes: bool = True,
    ) -> bytes | dict:
        """Synthesize speech and block until the audio is ready.

        Parameters
        ----------
        voice_id:
            ID of the cloned voice to use.
        text:
            Text to speak. Supports emotion tags:
            ``[excited]...[/excited]``, ``[slow]...[/slow]``, etc.
        language:
            BCP-47 language code. Defaults to ``"en"``.
        speed:
            Playback speed multiplier (0.5 – 2.0). Defaults to ``1.0``.
        return_bytes:
            If ``True`` (default) download and return the WAV bytes.
            If ``False`` return the raw job dict (including ``audio_url``).

        Returns
        -------
        bytes | dict
            Audio WAV bytes if ``return_bytes=True``, else job dict.
        """
        resp = self._session.post(
            self._url("/api/synthesize"),
            json={"voice_id": voice_id, "text": text,
                  "language": language, "speed": speed},
            timeout=self.timeout,
        )
        enqueued = self._check(resp)
        job      = self._poll_job(enqueued["job_id"])
        if not return_bytes:
            return job
        url = job.get("mp3_url") or job["audio_url"]
        return self._download_audio(url)

    # ── Batch ─────────────────────────────────────────────────────────────

    def batch(
        self,
        voice_id: str,
        lines: list[str],
        language: str = "en",
        speed: float = 1.0,
        *,
        download: bool = False,
    ) -> list[dict]:
        """Synthesize multiple lines in one batch job.

        Parameters
        ----------
        voice_id:
            Cloned voice to use for all lines.
        lines:
            Up to 50 non-empty strings to synthesize.
        language:
            BCP-47 language code. Defaults to ``"en"``.
        speed:
            Playback speed (0.5 – 2.0). Defaults to ``1.0``.
        download:
            If ``True``, fetch audio bytes for each completed item and
            attach as ``item["audio_bytes"]``.

        Returns
        -------
        list[dict]
            One dict per input line with keys:
            ``index``, ``text``, ``status``, ``audio_url``, ``filename``,
            ``watermark_id``.  If ``download=True``, also ``audio_bytes``.
        """
        resp = self._session.post(
            self._url("/api/batch"),
            json={"voice_id": voice_id, "lines": lines,
                  "language": language, "speed": speed},
            timeout=self.timeout,
        )
        enqueued = self._check(resp)
        batch    = self._poll_batch(enqueued["batch_id"])
        items    = batch.get("items", [])

        if download:
            for item in items:
                if item["status"] == "done" and item.get("audio_url"):
                    item["audio_bytes"] = self._download_audio(item["audio_url"])

        return items

    def download_batch_zip(self, voice_id: str, lines: list[str],
                            language: str = "en", speed: float = 1.0) -> bytes:
        """Run a batch job and return the ZIP of all audio clips."""
        resp = self._session.post(
            self._url("/api/batch"),
            json={"voice_id": voice_id, "lines": lines,
                  "language": language, "speed": speed},
            timeout=self.timeout,
        )
        enqueued = self._check(resp)
        batch    = self._poll_batch(enqueued["batch_id"])
        if not batch.get("zip_url"):
            raise MimicryError(500, "Batch completed but no ZIP was produced.")
        return self._download_audio(batch["zip_url"])

    # ── Watermark ─────────────────────────────────────────────────────────

    def verify(
        self,
        audio: Union[str, Path, bytes, BinaryIO],
        filename: str = "audio.wav",
    ) -> dict:
        """Extract the Mimicry watermark from an audio file.

        Returns
        -------
        dict
            ``{"watermark_found": bool, "watermark_id": str | None, ...}``
        """
        if isinstance(audio, (str, Path)):
            with open(audio, "rb") as f:
                data = f.read()
            filename = Path(audio).name
        elif isinstance(audio, bytes):
            data = audio
        else:
            data = audio.read()

        resp = self._session.post(
            self._url("/api/verify"),
            files={"audio": (filename, io.BytesIO(data))},
            timeout=self.timeout,
        )
        return self._check(resp)

    # ── History / Queue ───────────────────────────────────────────────────

    def history(self) -> list[dict]:
        """Return the last 40 synthesis records."""
        return self._check(
            self._session.get(self._url("/api/history"), timeout=self.timeout)
        )

    def queue(self) -> list[dict]:
        """Return all active and recent synth/batch jobs."""
        return self._check(
            self._session.get(self._url("/api/queue"), timeout=self.timeout)
        )

    # ── Context manager ───────────────────────────────────────────────────

    def __enter__(self) -> "Mimicry":
        return self

    def __exit__(self, *_: Any) -> None:
        self._session.close()

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()
