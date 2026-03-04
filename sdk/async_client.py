"""
Mimicry Async Python SDK
========================
An asyncio-native client for the Mimicry API using httpx.

Quickstart
----------
    import asyncio
    from sdk.async_client import AsyncMimicry

    async def main():
        async with AsyncMimicry("http://localhost:8000") as m:
            voices = await m.list_voices()
            voice  = await m.upload_voice("Alice", "alice.wav")
            audio  = await m.synthesize(voice["id"], "Hello from async Mimicry!")
            with open("output.wav", "wb") as f:
                f.write(audio)

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any, BinaryIO, Union

try:
    import httpx
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "AsyncMimicry requires 'httpx'. Install it with: pip install httpx"
    ) from exc

from .client import MimicryError, JobTimeoutError


class AsyncMimicry:
    """Async Mimicry API client (asyncio + httpx).

    Parameters
    ----------
    base_url:
        Root URL of the Mimicry server, e.g. ``"http://localhost:8000"``.
    api_key:
        Optional Bearer token (set ``MIMICRY_API_KEY`` on the server to require it).
    timeout:
        HTTP request timeout in seconds (default 60).
    poll_interval:
        Seconds between job status polls (default 2.5).
    job_timeout:
        Maximum seconds to wait for a synthesis job (default 300).
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
        self.poll_interval = poll_interval
        self.job_timeout   = job_timeout
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )

    # ── Internals ─────────────────────────────────────────────────────────

    def _check(self, resp: httpx.Response) -> dict:
        if resp.is_error:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise MimicryError(resp.status_code, detail)
        return resp.json()

    async def _poll_job(self, job_id: str) -> dict:
        deadline = asyncio.get_event_loop().time() + self.job_timeout
        while True:
            resp = await self._client.get(f"/api/jobs/{job_id}")
            job  = self._check(resp)
            if job["status"] == "done":
                return job
            if job["status"] == "failed":
                raise MimicryError(500, f"Job {job_id} failed: {job.get('error')}")
            if asyncio.get_event_loop().time() > deadline:
                raise JobTimeoutError(
                    f"Job {job_id} did not finish within {self.job_timeout}s"
                )
            await asyncio.sleep(self.poll_interval)

    async def _poll_batch(self, batch_id: str) -> dict:
        deadline = asyncio.get_event_loop().time() + self.job_timeout
        while True:
            resp  = await self._client.get(f"/api/batch/{batch_id}")
            batch = self._check(resp)
            if batch["status"] == "done":
                return batch
            if batch["status"] == "failed":
                raise MimicryError(500, f"Batch {batch_id} failed: {batch.get('error')}")
            if asyncio.get_event_loop().time() > deadline:
                raise JobTimeoutError(
                    f"Batch {batch_id} did not finish within {self.job_timeout}s"
                )
            await asyncio.sleep(self.poll_interval)

    async def _download(self, url: str) -> bytes:
        full = url if url.startswith("http") else url
        resp = await self._client.get(full)
        if resp.is_error:
            raise MimicryError(resp.status_code, f"Audio download failed: {resp.text}")
        return resp.content

    # ── Status ────────────────────────────────────────────────────────────

    async def status(self) -> dict:
        return self._check(await self._client.get("/api/status"))

    async def wait_for_model(self, timeout: float = 120.0, interval: float = 3.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            try:
                s = await self.status()
                if s.get("model_loaded"):
                    return
            except (httpx.ConnectError, MimicryError):
                pass
            if asyncio.get_event_loop().time() > deadline:
                raise JobTimeoutError(f"Model did not load within {timeout}s")
            await asyncio.sleep(interval)

    # ── Voices ────────────────────────────────────────────────────────────

    async def list_voices(self) -> list[dict]:
        return self._check(await self._client.get("/api/voices"))

    async def upload_voice(
        self,
        name: str,
        audio: Union[str, Path, bytes, BinaryIO],
        ref_text: str | None = None,
        filename: str = "reference.wav",
    ) -> dict:
        if isinstance(audio, (str, Path)):
            with open(audio, "rb") as f:
                data = f.read()
            filename = Path(audio).name
        elif isinstance(audio, bytes):
            data = audio
        else:
            data = audio.read()

        fields: dict[str, Any] = {"name": name}
        if ref_text is not None:
            fields["ref_text"] = ref_text

        resp = await self._client.post(
            "/api/voices",
            data=fields,
            files={"audio": (filename, io.BytesIO(data))},
        )
        return self._check(resp)

    async def delete_voice(self, voice_id: str) -> dict:
        return self._check(await self._client.delete(f"/api/voices/{voice_id}"))

    async def update_ref_text(self, voice_id: str, ref_text: str) -> dict:
        return self._check(await self._client.patch(
            f"/api/voices/{voice_id}", json={"ref_text": ref_text}
        ))

    async def export_voice(self, voice_id: str) -> bytes:
        resp = await self._client.get(f"/api/voices/{voice_id}/export")
        if resp.is_error:
            raise MimicryError(resp.status_code, resp.text)
        return resp.content

    async def import_voice(
        self,
        data: Union[str, Path, bytes, BinaryIO],
        filename: str = "voice.mimicry",
    ) -> dict:
        if isinstance(data, (str, Path)):
            with open(data, "rb") as f:
                raw = f.read()
            filename = Path(data).name
        elif isinstance(data, bytes):
            raw = data
        else:
            raw = data.read()
        resp = await self._client.post(
            "/api/voices/import",
            files={"file": (filename, io.BytesIO(raw))},
        )
        return self._check(resp)

    async def mix(
        self,
        voice_id_a: str,
        voice_id_b: str,
        name: str,
        alpha: float = 0.5,
    ) -> dict:
        return self._check(await self._client.post(
            "/api/voices/mix",
            json={"voice_id_a": voice_id_a, "voice_id_b": voice_id_b,
                  "alpha": alpha, "name": name},
        ))

    # ── Synthesis ─────────────────────────────────────────────────────────

    async def synthesize(
        self,
        voice_id: str,
        text: str,
        language: str = "en",
        speed: float = 1.0,
        *,
        return_bytes: bool = True,
    ) -> bytes | dict:
        """Synthesize speech asynchronously, blocking until ready."""
        resp     = await self._client.post(
            "/api/synthesize",
            json={"voice_id": voice_id, "text": text,
                  "language": language, "speed": speed},
        )
        enqueued = self._check(resp)
        job      = await self._poll_job(enqueued["job_id"])
        if not return_bytes:
            return job
        url = job.get("mp3_url") or job["audio_url"]
        return await self._download(url)

    # ── Batch ─────────────────────────────────────────────────────────────

    async def batch(
        self,
        voice_id: str,
        lines: list[str],
        language: str = "en",
        speed: float = 1.0,
        *,
        download: bool = False,
    ) -> list[dict]:
        resp     = await self._client.post(
            "/api/batch",
            json={"voice_id": voice_id, "lines": lines,
                  "language": language, "speed": speed},
        )
        enqueued = self._check(resp)
        result   = await self._poll_batch(enqueued["batch_id"])
        items    = result.get("items", [])
        if download:
            tasks = []
            for item in items:
                if item["status"] == "done" and item.get("audio_url"):
                    tasks.append(self._download(item["audio_url"]))
                else:
                    tasks.append(asyncio.coroutine(lambda: None)())
            downloaded = await asyncio.gather(*tasks, return_exceptions=True)
            for item, audio in zip(items, downloaded):
                if isinstance(audio, bytes):
                    item["audio_bytes"] = audio
        return items

    async def download_batch_zip(
        self,
        voice_id: str,
        lines: list[str],
        language: str = "en",
        speed: float = 1.0,
    ) -> bytes:
        resp     = await self._client.post(
            "/api/batch",
            json={"voice_id": voice_id, "lines": lines,
                  "language": language, "speed": speed},
        )
        enqueued = self._check(resp)
        result   = await self._poll_batch(enqueued["batch_id"])
        if not result.get("zip_url"):
            raise MimicryError(500, "Batch completed but no ZIP was produced.")
        return await self._download(result["zip_url"])

    # ── Watermark ─────────────────────────────────────────────────────────

    async def verify(
        self,
        audio: Union[str, Path, bytes, BinaryIO],
        filename: str = "audio.wav",
    ) -> dict:
        if isinstance(audio, (str, Path)):
            with open(audio, "rb") as f:
                data = f.read()
            filename = Path(audio).name
        elif isinstance(audio, bytes):
            data = audio
        else:
            data = audio.read()
        resp = await self._client.post(
            "/api/verify",
            files={"audio": (filename, io.BytesIO(data))},
        )
        return self._check(resp)

    # ── History / Queue ───────────────────────────────────────────────────

    async def history(self) -> list[dict]:
        return self._check(await self._client.get("/api/history"))

    async def queue(self) -> list[dict]:
        return self._check(await self._client.get("/api/queue"))

    # ── Context manager ───────────────────────────────────────────────────

    async def __aenter__(self) -> "AsyncMimicry":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._client.aclose()

    async def close(self) -> None:
        await self._client.aclose()
