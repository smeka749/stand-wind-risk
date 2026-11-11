"""S3 artifacts, under quotes/<quote_id>/ in S3_BUCKET: case/, score.json, video.mp4.

With S3_BUCKET set, runners pass s3:// keys between stages and each stage
downloads, runs locally, and uploads. Unset, runners use local paths and never
import boto3. The client is injectable so tests use an in-memory fake.

A case is ~140 files, so directories transfer through a thread pool
(S3_TRANSFER_WORKERS, default 32) with a matching connection pool.
"""
from __future__ import annotations

import fnmatch
import io
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .logging_utils import get_logger, log_event

_logger = get_logger("wind_risk.s3")


def transfer_workers() -> int:
    return max(1, int(os.environ.get("S3_TRANSFER_WORKERS", "32")))


def _parallel(fn, items) -> None:
    """Run fn over items on the transfer pool; the first error is raised."""
    with ThreadPoolExecutor(max_workers=transfer_workers()) as pool:
        for _ in pool.map(fn, items):
            pass


def is_s3_key(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith("s3://")


def parse_s3_key(value: str) -> tuple[str, str]:
    """'s3://bucket/some/key' -> ('bucket', 'some/key')."""
    rest = value[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"not a usable s3:// key: {value!r}")
    return bucket, key


def s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key.lstrip('/')}"


def case_prefix(quote_id: str) -> str:
    return f"quotes/{quote_id}/case"


def video_key(quote_id: str) -> str:
    return f"quotes/{quote_id}/video.mp4"


def score_key(quote_id: str) -> str:
    return f"quotes/{quote_id}/score.json"


def resolve_bucket(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    return os.environ.get("S3_BUCKET") or None


def get_s3_client():
    """Lazily build a boto3 S3 client. Only called in S3 mode."""
    import boto3
    from botocore.config import Config

    # Default pool is 10 connections, which would cap parallel transfers.
    return boto3.client("s3", config=Config(max_pool_connections=transfer_workers()))


def upload_file(client, bucket: str, local_path: str | Path, key: str) -> str:
    data = Path(local_path).read_bytes()
    client.put_object(Bucket=bucket, Key=key, Body=data)
    return s3_uri(bucket, key)


def upload_dir(client, bucket: str, local_dir: str | Path, prefix: str,
               exclude: tuple[str, ...] = ()) -> str:
    """Upload a tree under prefix; return the s3:// prefix. ``exclude`` globs
    match the top-level path component, e.g. "processor*"."""
    local_dir = Path(local_dir)
    root = prefix.rstrip("/")
    files = [p for p in sorted(local_dir.rglob("*")) if p.is_file()
             and not any(fnmatch.fnmatch(p.relative_to(local_dir).parts[0], pat)
                         for pat in exclude)]

    def put(path: Path) -> None:
        key = f"{root}/{path.relative_to(local_dir).as_posix()}"
        client.put_object(Bucket=bucket, Key=key, Body=path.read_bytes())

    start = time.monotonic()
    _parallel(put, files)
    log_event(_logger, "s3_transfer", direction="upload", prefix=root,
              files=len(files), bytes=sum(p.stat().st_size for p in files),
              seconds=round(time.monotonic() - start, 3))
    return s3_uri(bucket, prefix)


def download_dir(client, bucket: str, prefix: str, local_dir: str | Path) -> Path:
    """Download everything under ``prefix`` into ``local_dir``."""
    local_dir = Path(local_dir)
    local_dir.mkdir(parents=True, exist_ok=True)
    root = prefix.rstrip("/") + "/"
    keys: list[str] = []
    token = None
    while True:
        kwargs: dict = {"Bucket": bucket, "Prefix": root}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        keys += [o["Key"] for o in resp.get("Contents", []) if o["Key"] != root]
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")

    sizes: list[int] = []

    def get(key: str) -> None:
        dest = local_dir / key[len(root):]
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = client.get_object(Bucket=bucket, Key=key)["Body"]
        data = body.read() if hasattr(body, "read") else body
        dest.write_bytes(data)
        sizes.append(len(data))

    start = time.monotonic()
    _parallel(get, keys)
    log_event(_logger, "s3_transfer", direction="download", prefix=root.rstrip("/"),
              files=len(keys), bytes=sum(sizes),
              seconds=round(time.monotonic() - start, 3))
    return local_dir


def download_file(client, bucket: str, key: str, local_path: str | Path) -> Path:
    local_path = Path(local_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    local_path.write_bytes(body.read() if hasattr(body, "read") else body)
    return local_path


def make_presigner(bucket: str, client=None, expiry_seconds: int = 3600):
    """Return presigner(key) -> short-lived HTTPS URL. key may be s3:// or bare."""
    if client is None:
        client = get_s3_client()

    def presign(key: str) -> str:
        if is_s3_key(key):
            _bucket, s3key = parse_s3_key(key)
        else:
            _bucket, s3key = bucket, key.lstrip("/")
        return client.generate_presigned_url(
            "get_object",
            Params={"Bucket": _bucket, "Key": s3key},
            ExpiresIn=expiry_seconds,
        )

    return presign
