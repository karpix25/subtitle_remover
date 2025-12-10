from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from .video_processor import VideoProcessingOptions, VideoProcessor

app = FastAPI(title="Subtitle Cleaner", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
processor = VideoProcessor()


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/clean")
async def clean_video(
    file: UploadFile = File(...),
    max_resolution: int = Form(1080),
    inpaint_radius: int = Form(4),
    subtitle_intensity_threshold: Optional[float] = Form(None),
) -> JSONResponse:
    if file.content_type is None or not file.content_type.startswith("video"):
        raise HTTPException(status_code=400, detail="Expected video file upload")

    start_time = time.perf_counter()
    options = VideoProcessingOptions(
        max_resolution=max_resolution,
        inpaint_radius=inpaint_radius,
        subtitle_intensity_threshold=subtitle_intensity_threshold,
    )

    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename or "video").suffix) as tmp_in:
        shutil.copyfileobj(file.file, tmp_in)
        input_path = Path(tmp_in.name)

    try:
        output_path = OUTPUT_DIR / f"cleaned_{int(time.time() * 1000)}.mp4"
        stats = await asyncio.get_event_loop().run_in_executor(
            None, lambda: processor.process_video(input_path, output_path, options)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Video processing failed")
        raise HTTPException(status_code=500, detail="Processing failed") from exc
    finally:
        input_path.unlink(missing_ok=True)

    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    payload = {
        "status": "ok",
        "video_url": str(output_path.resolve()),
        "time_ms": elapsed_ms,
        "stats": stats,
    }
    return JSONResponse(payload)


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
