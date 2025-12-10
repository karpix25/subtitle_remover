# CPU Subtitle Cleaner

FastAPI service for removing subtitle overlays from trading videos using PaddleOCR for text detection and OpenCV inpainting. The service runs entirely on CPU and exposes HTTP endpoints for full-video cleaning plus preview utilities.

## Features
- PaddleOCR-based text detection with heuristic subtitle classification tuned for market UIs
- CPU-only Navier-Stokes inpainting via OpenCV
- Streaming frame pipeline without intermediate disk writes
- `/clean`, `/preview`, `/health` HTTP endpoints
- Docker image ready for EasyPanel deployments (port 8000)

## Project Layout
```
├── app/
│   ├── main.py
│   ├── video_processor.py
│   ├── text_detector.py
│   ├── classifier.py
│   ├── mask_builder.py
│   ├── inpainter.py
│   └── ffmpeg_utils.py
├── models/
│   └── subtitle_rules.json
├── requirements.txt
└── Dockerfile
```

## Local Development
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## API
### `POST /clean`
- Provide either a multipart `file` upload or a `file_url` (HTTP/HTTPS) plus optional form fields: `max_resolution`, `inpaint_radius`, `subtitle_intensity_threshold`, and `callback_url`.
- Files larger than **100 MB** are rejected during upload/download before processing starts.
- The endpoint responds immediately with `{ "status": "accepted", "task_id": "<id>" }` while work continues in the background queue.
- When processing finishes the task result includes timing, cleaning stats, and the final download link (local path by default or an S3 URL when storage is configured). If `callback_url` is provided, the same payload is POSTed to that URL.

### `GET /tasks/{task_id}`
Poll task status (`pending`, `processing`, `completed`, `failed`) and retrieve the finished payload using the `task_id` returned from `/clean`.

### `POST /preview`
Returns single-frame before/after PNGs (base64) plus mask for debugging heuristics.

### `GET /health`
Simple readiness probe.

## S3 Uploads
By default cleaned videos are written to the local `output/` folder. To automatically upload them to object storage and receive a downloadable link, configure the following environment variables:

- `S3_BUCKET` (required): target bucket name.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (required unless you rely on instance roles).
- `AWS_DEFAULT_REGION` or `S3_REGION`: region used for signing/URL building.
- `S3_PREFIX` (optional): key prefix such as `cleaned-videos`.
- `S3_ENDPOINT_URL` (optional): custom endpoint for S3-compatible storage (e.g., MinIO).
- `S3_FORCE_PATH_STYLE` (optional): set to `true` for providers that require path-style URLs.
- `S3_OBJECT_ACL` (optional): ACL to apply (e.g., `public-read`).
- `S3_PUBLIC_BASE_URL` (optional): base URL for already-public buckets, e.g. `https://cdn.example.com/videos`.
- `S3_PRESIGN_SECONDS` (optional): if set to a positive integer, the API returns a presigned URL that expires after the provided number of seconds.

When `S3_BUCKET` is set, `/clean` uploads the video to the bucket, deletes the local artifact, and returns the resulting URL (either presigned or public, depending on your settings).

## Docker
Build and run:
```bash
docker build -t subtitle-cleaner .
docker run -p 8000:8000 subtitle-cleaner
```

EasyPanel automatically exposes port 8000.

## Testing Checklist
1. Trading chart with dynamic subtitles: subtitles removed, tooltips/UI intact.
2. Top-aligned subtitles with motion: cleaned without dents.
3. Subtitles over grid lines: Navier-Stokes restores lines.
4. Video without subtitles: frames unchanged (mask stays zero).
