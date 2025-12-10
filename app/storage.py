from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger


@dataclass(frozen=True)
class S3Settings:
    bucket: Optional[str]
    prefix: Optional[str]
    endpoint_url: Optional[str]
    region_name: Optional[str]
    public_base_url: Optional[str]
    presign_ttl: Optional[int]
    force_path_style: bool
    object_acl: Optional[str]

    @property
    def enabled(self) -> bool:
        return bool(self.bucket)


def _clean_prefix(raw_prefix: Optional[str]) -> Optional[str]:
    if not raw_prefix:
        return None
    trimmed = raw_prefix.strip().strip("/")
    return trimmed or None


@lru_cache(maxsize=1)
def get_s3_settings() -> S3Settings:
    presign_ttl: Optional[int] = None
    presign_env = os.getenv("S3_PRESIGN_SECONDS")
    if presign_env:
        try:
            parsed = int(presign_env)
            if parsed > 0:
                presign_ttl = parsed
        except ValueError:
            logger.warning("Invalid S3_PRESIGN_SECONDS=%s; ignoring.", presign_env)

    return S3Settings(
        bucket=os.getenv("S3_BUCKET"),
        prefix=_clean_prefix(os.getenv("S3_PREFIX")),
        endpoint_url=os.getenv("S3_ENDPOINT_URL"),
        region_name=os.getenv("S3_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        public_base_url=os.getenv("S3_PUBLIC_BASE_URL"),
        presign_ttl=presign_ttl,
        force_path_style=os.getenv("S3_FORCE_PATH_STYLE", "false").lower() in {"1", "true", "yes"},
        object_acl=os.getenv("S3_OBJECT_ACL"),
    )


def is_s3_enabled() -> bool:
    return get_s3_settings().enabled


@lru_cache(maxsize=1)
def _get_s3_client(settings: S3Settings):
    if not settings.enabled:
        raise RuntimeError("S3_BUCKET is not configured.")

    boto_kwargs: dict[str, object] = {}
    if settings.region_name:
        boto_kwargs["region_name"] = settings.region_name
    if settings.endpoint_url:
        boto_kwargs["endpoint_url"] = settings.endpoint_url

    if settings.force_path_style:
        boto_kwargs["config"] = BotoConfig(s3={"addressing_style": "path"})

    session = boto3.session.Session()
    return session.client("s3", **boto_kwargs)


def _build_object_key(local_path: Path, settings: S3Settings) -> str:
    filename = local_path.name
    if settings.prefix:
        return f"{settings.prefix}/{filename}"
    return filename


def _build_public_url(key: str, settings: S3Settings) -> str:
    if settings.public_base_url:
        base = settings.public_base_url.rstrip("/")
        return f"{base}/{key}"

    if settings.endpoint_url:
        endpoint = settings.endpoint_url.rstrip("/")
        return f"{endpoint}/{settings.bucket}/{key}"

    region = settings.region_name or "us-east-1"
    if region == "us-east-1":
        return f"https://{settings.bucket}.s3.amazonaws.com/{key}"
    return f"https://{settings.bucket}.s3.{region}.amazonaws.com/{key}"


def upload_video_and_get_url(local_path: Path, content_type: str = "video/mp4") -> str:
    """
    Uploads the processed video to the configured S3 bucket and returns a URL that can be shared
    with the caller. Falls back to presigned URLs when the bucket is private.
    """
    settings = get_s3_settings()
    if not settings.enabled:
        raise RuntimeError("S3 storage is not configured. Set S3_BUCKET to enable uploads.")

    client = _get_s3_client(settings)
    object_key = _build_object_key(Path(local_path), settings)
    extra_args: dict[str, str] = {}
    if content_type:
        extra_args["ContentType"] = content_type
    if settings.object_acl:
        extra_args["ACL"] = settings.object_acl

    try:
        client.upload_file(
            Filename=str(local_path),
            Bucket=settings.bucket,
            Key=object_key,
            ExtraArgs=extra_args or None,
        )
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - network failure
        logger.exception("Failed to upload %s to S3", local_path)
        raise

    if settings.presign_ttl:
        return client.generate_presigned_url(
            ClientMethod="get_object",
            Params={"Bucket": settings.bucket, "Key": object_key},
            ExpiresIn=settings.presign_ttl,
        )

    return _build_public_url(object_key, settings)
