from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from .storage import is_s3_enabled, upload_video_and_get_url
from .task_manager import TaskManager
from .video_processor import VideoProcessingOptions, VideoProcessor

MAX_FILE_SIZE_MB = 100
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
STREAM_CHUNK_SIZE = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = float(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "120"))

app = FastAPI(title="Subtitle Cleaner", version="0.2.0")
app.add_middleware(
    CORSMiddleware(
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
)

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
processor = VideoProcessor()
task_manager = TaskManager()


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/clean")
async def clean_video(
    file: Optional[UploadFile] = File(None),
    file_url: Optional[str] = Form(None),
    max_resolution: int = Form(1080),
    inpaint_radius: int = Form(4),
    subtitle_intensity_threshold: Optional[float] = Form(None),
    callback_url: Optional[str] = Form(None),
) -> JSONResponse:
    if file is None and not file_url:
        raise HTTPException(status_code=400, detail="Either file or file_url must be provided")

    if file is not None and (file.content_type is None or not file.content_type.startswith("video")):
        raise HTTPException(status_code=400, detail="Expected video file upload")

    if file_url and not file_url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="file_url must be http(s)")

    options = VideoProcessingOptions(
        max_resolution=max_resolution,
        inpaint_radius=inpaint_radius,
        subtitle_intensity_threshold=subtitle_intensity_threshold,
    )

    try:
        input_path = await _persist_input(file=file, file_url=file_url)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to persist input file")
        raise HTTPException(status_code=500, detail="Failed to persist input file") from exc

    task_id = task_manager.create_task(callback_url=callback_url)
    output_path = OUTPUT_DIR / f"cleaned_{task_id}.mp4"
    loop = asyncio.get_event_loop()
    try:
        future = loop.run_in_executor(
            None,
            lambda: _process_async_task(
                task_id=task_id,
                input_path=input_path,
                output_path=output_path,
                options=options,
                callback_url=callback_url,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        input_path.unlink(missing_ok=True)
        task_manager.mark_failed(task_id, "Failed to schedule processing")
        logger.exception("Unable to schedule background task %s", task_id)
        raise HTTPException(status_code=500, detail="Failed to schedule processing") from exc
    future.add_done_callback(_log_future_exception)

    return JSONResponse({"status": "accepted", "task_id": task_id})


@app.post("/preview")
async def preview_frame(
    file: UploadFile = File(...),
    frame_number: int = Form(0),
    max_resolution: int = Form(720),
    inpaint_radius: int = Form(4),
):
    """Optional helper endpoint that returns a single before/after frame pair."""
    if frame_number < 0:
        raise HTTPException(status_code=400, detail="frame_number must be >= 0")

    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename or "video").suffix) as tmp_in:
        shutil.copyfileobj(file.file, tmp_in)
        input_path = Path(tmp_in.name)

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: processor.preview_frame(
                input_path,
                frame_number,
                VideoProcessingOptions(max_resolution=max_resolution, inpaint_radius=inpaint_radius),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Preview generation failed")
        raise HTTPException(status_code=500, detail="Preview failed") from exc
    finally:
        input_path.unlink(missing_ok=True)

    return JSONResponse(result)


@app.get("/tasks/{task_id}")
def get_task(task_id: str) -> JSONResponse:
    record = task_manager.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(record)


async def _persist_input(*, file: Optional[UploadFile], file_url: Optional[str]) -> Path:
    if file is not None:
        return await _save_uploaded_file(file)
    if file_url:
        return await _download_file(file_url)
    raise HTTPException(status_code=400, detail="Missing input file")


async def _save_uploaded_file(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "video").suffix or ".mp4"
    await upload.seek(0)
    total_bytes = 0
    tmp_handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_handle.close()
    tmp_path = Path(tmp_handle.name)
    try:
        with tmp_path.open("wb") as dst:
            while True:
                chunk = await upload.read(STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_FILE_SIZE_BYTES:
                    raise HTTPException(status_code=400, detail="File too large (limit 100 MB)")
                dst.write(chunk)
    finally:
        await upload.close()

    if total_bytes == 0:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    return tmp_path


async def _download_file(file_url: str) -> Path:
    parsed = urlparse(file_url)
    suffix = Path(parsed.path).suffix or ".mp4"
    tmp_handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_handle.close()
    tmp_path = Path(tmp_handle.name)
    async with httpx.AsyncClient(timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS)) as client:
        try:
            async with client.stream("GET", file_url, follow_redirects=True) as response:
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > MAX_FILE_SIZE_BYTES:
                            raise HTTPException(status_code=400, detail="File too large (limit 100 MB)")
                    except ValueError:
                        logger.warning("Invalid Content-Length from %s: %s", file_url, content_length)
                total_bytes = 0
                with tmp_path.open("wb") as dst:
                    async for chunk in response.aiter_bytes(STREAM_CHUNK_SIZE):
                        total_bytes += len(chunk)
                        if total_bytes > MAX_FILE_SIZE_BYTES:
                            raise HTTPException(status_code=400, detail="File too large (limit 100 MB)")
                        dst.write(chunk)
        except HTTPException:
            tmp_path.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001
            tmp_path.unlink(missing_ok=True)
            logger.exception("Failed to download %s", file_url)
            raise HTTPException(status_code=400, detail="Unable to download file") from exc

    if total_bytes == 0:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Downloaded file is empty")

    return tmp_path


def _process_async_task(
    *,
    task_id: str,
    input_path: Path,
    output_path: Path,
    options: VideoProcessingOptions,
    callback_url: Optional[str],
) -> None:
    start_time = time.perf_counter()
    task_manager.mark_processing(task_id)
    try:
        stats = processor.process_video(input_path, output_path, options)
        video_url = _finalize_output_file(output_path)
        elapsed_ms = int((time.perf_counter() - start_time) * 1000)
        payload = {
            "task_id": task_id,
            "status": "completed",
            "video_url": video_url,
            "time_ms": elapsed_ms,
            "stats": stats,
        }
        task_manager.mark_completed(task_id, payload)
        if callback_url:
            _post_callback(callback_url, payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Background processing failed for task %s", task_id)
        error_message = str(exc)
        failure_payload = {
            "task_id": task_id,
            "status": "failed",
            "error": error_message,
        }
        task_manager.mark_failed(task_id, error_message)
        if callback_url:
            _post_callback(callback_url, failure_payload)
    finally:
        input_path.unlink(missing_ok=True)
        if output_path.exists() and not is_s3_enabled():
            output_path.unlink(missing_ok=True)


def _finalize_output_file(output_path: Path) -> str:
    video_url = str(output_path.resolve())
    if is_s3_enabled():
        video_url = upload_video_and_get_url(output_path)
        output_path.unlink(missing_ok=True)
    return video_url


def _post_callback(url: str, payload: dict) -> None:
    try:
        response = httpx.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.error("Callback to %s failed: %s", url, exc)


def _log_future_exception(future) -> None:
    try:
        future.result()
    except Exception as exc:  # noqa: BLE001
        logger.error("Background task raised: %s", exc)
