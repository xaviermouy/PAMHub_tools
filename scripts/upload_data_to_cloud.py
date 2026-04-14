#!/usr/bin/env python3
"""
s3_sync.py — Throttled S3 sync using boto3
Replicates `aws s3 sync` with concurrency control, progress tracking,
resume support, and per-file logging.

Usage:
    python s3_sync.py [--local-dir PATH] [--bucket BUCKET] [--prefix PREFIX]
                      [--concurrency N] [--dry-run] [--extensions .wav .flac]
"""

import os
import sys
import hashlib
import logging
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError
from tqdm import tqdm

# ── Defaults (edit here or override via CLI args) ─────────────────────────────
DEFAULT_LOCAL_DIR  = "/media/xmouy/Elements/Wellfleet backups/"
DEFAULT_BUCKET     = "neracoos-pam-data-ingest"
DEFAULT_PREFIX     = "Wellfleet/"
DEFAULT_CONCURRENCY = 5          # concurrent multipart threads per file
DEFAULT_MAX_WORKERS = 3          # concurrent file uploads
MULTIPART_THRESHOLD = 64 * 1024 * 1024   # 64 MB
MULTIPART_CHUNKSIZE = 16 * 1024 * 1024   # 16 MB
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("s3_sync.log"),
    ],
)
log = logging.getLogger(__name__)


def build_transfer_config(concurrency: int) -> TransferConfig:
    return TransferConfig(
        max_concurrency=concurrency,
        multipart_threshold=MULTIPART_THRESHOLD,
        multipart_chunksize=MULTIPART_CHUNKSIZE,
        use_threads=True,
    )


def list_local_files(local_dir: Path, extensions: list[str] | None = None) -> list[Path]:
    """Recursively list files, optionally filtered by extension."""
    files = [p for p in local_dir.rglob("*") if p.is_file()]
    if extensions:
        exts = {e.lower() for e in extensions}
        files = [p for p in files if p.suffix.lower() in exts]
    return sorted(files)


def list_s3_objects(s3_client, bucket: str, prefix: str) -> dict[str, dict]:
    """Return {s3_key: {size, etag}} for all objects under prefix."""
    objects = {}
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            objects[obj["Key"]] = {
                "size": obj["Size"],
                "etag": obj["ETag"].strip('"'),
            }
    return objects


def local_etag(path: Path, chunk_size: int = MULTIPART_CHUNKSIZE) -> str:
    """
    Compute the ETag AWS would assign — supports both single and multipart uploads.
    For files smaller than MULTIPART_THRESHOLD, it's a plain MD5.
    For larger files it's MD5-of-MD5s with the part count appended.
    """
    file_size = path.stat().st_size
    if file_size < MULTIPART_THRESHOLD:
        md5 = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                md5.update(chunk)
        return md5.hexdigest()
    else:
        part_md5s = []
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                part_md5s.append(hashlib.md5(chunk).digest())
        combined = hashlib.md5(b"".join(part_md5s)).hexdigest()
        return f"{combined}-{len(part_md5s)}"


def needs_upload(local_path: Path, s3_key: str, s3_objects: dict) -> bool:
    """Return True if the file is missing or different on S3."""
    if s3_key not in s3_objects:
        return True
    remote = s3_objects[s3_key]
    if local_path.stat().st_size != remote["size"]:
        return True
    # ETag check (handles multipart correctly)
    return local_etag(local_path) != remote["etag"]


def upload_file(
    s3_client,
    local_path: Path,
    bucket: str,
    s3_key: str,
    transfer_config: TransferConfig,
    progress_bar: tqdm,
    lock: Lock,
    dry_run: bool = False,
) -> tuple[str, bool, str]:
    """Upload a single file. Returns (s3_key, success, error_msg)."""
    if dry_run:
        with lock:
            progress_bar.update(local_path.stat().st_size)
        return s3_key, True, ""
    try:
        file_size = local_path.stat().st_size

        def _progress_callback(bytes_transferred):
            with lock:
                progress_bar.update(bytes_transferred)

        s3_client.upload_file(
            Filename=str(local_path),
            Bucket=bucket,
            Key=s3_key,
            Config=transfer_config,
            Callback=_progress_callback,
        )
        return s3_key, True, ""
    except ClientError as e:
        return s3_key, False, str(e)
    except Exception as e:
        return s3_key, False, str(e)


def sync(
    local_dir: str,
    bucket: str,
    prefix: str,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_workers: int = DEFAULT_MAX_WORKERS,
    extensions: list[str] | None = None,
    dry_run: bool = False,
):
    local_dir = Path(local_dir)
    if not local_dir.exists():
        log.error(f"Local directory not found: {local_dir}")
        sys.exit(1)

    log.info(f"Source : {local_dir}")
    log.info(f"Target : s3://{bucket}/{prefix}")
    log.info(f"Concurrency: {concurrency} threads/file, {max_workers} parallel files")
    if dry_run:
        log.info("DRY RUN — no files will be uploaded")

    s3 = boto3.client("s3")
    transfer_config = build_transfer_config(concurrency)

    log.info("Scanning local files...")
    local_files = list_local_files(local_dir, extensions)
    log.info(f"Found {len(local_files):,} local file(s)")

    log.info("Listing existing S3 objects...")
    s3_objects = list_s3_objects(s3, bucket, prefix)
    log.info(f"Found {len(s3_objects):,} existing S3 object(s)")

    # Determine what needs uploading
    to_upload = []
    skipped = 0
    for local_path in local_files:
        rel = local_path.relative_to(local_dir)
        s3_key = prefix + rel.as_posix()
        if needs_upload(local_path, s3_key, s3_objects):
            to_upload.append((local_path, s3_key))
        else:
            skipped += 1

    total_bytes = sum(p.stat().st_size for p, _ in to_upload)
    log.info(f"To upload : {len(to_upload):,} file(s)  ({total_bytes / 1e9:.2f} GB)")
    log.info(f"Skipped   : {skipped:,} file(s) already in sync")

    if not to_upload:
        log.info("Nothing to do — all files are up to date.")
        return

    lock = Lock()
    succeeded, failed = 0, 0

    with tqdm(
        total=total_bytes,
        unit="B",
        unit_scale=True,
        unit_divisor=1024,
        desc="Uploading",
        ncols=80,
    ) as pbar:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    upload_file,
                    s3, local_path, bucket, s3_key,
                    transfer_config, pbar, lock, dry_run,
                ): s3_key
                for local_path, s3_key in to_upload
            }
            for future in as_completed(futures):
                s3_key, ok, err = future.result()
                if ok:
                    succeeded += 1
                    log.debug(f"OK  {s3_key}")
                else:
                    failed += 1
                    log.error(f"FAIL  {s3_key} — {err}")

    log.info(f"Done. Uploaded: {succeeded:,}  Failed: {failed:,}  Skipped: {skipped:,}")
    if failed:
        log.warning(f"{failed} file(s) failed — check s3_sync.log for details")
        sys.exit(1)


def parse_args():
    p = argparse.ArgumentParser(description="Throttled S3 sync with boto3")
    p.add_argument("--local-dir",    default=DEFAULT_LOCAL_DIR)
    p.add_argument("--bucket",       default=DEFAULT_BUCKET)
    p.add_argument("--prefix",       default=DEFAULT_PREFIX)
    p.add_argument("--concurrency",  type=int, default=DEFAULT_CONCURRENCY,
                   help="Multipart threads per file (default: 5)")
    p.add_argument("--max-workers",  type=int, default=DEFAULT_MAX_WORKERS,
                   help="Parallel file uploads (default: 3)")
    p.add_argument("--extensions",   nargs="*", default=None,
                   help="Only sync these extensions, e.g. --extensions .wav .flac")
    p.add_argument("--dry-run",      action="store_true",
                   help="List what would be uploaded without uploading")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sync(
        local_dir=args.local_dir,
        bucket=args.bucket,
        prefix=args.prefix,
        concurrency=args.concurrency,
        max_workers=args.max_workers,
        extensions=args.extensions,
        dry_run=args.dry_run,
    )

# pip install boto3 tqdm
#
# # default settings (edit the DEFAULT_* vars at the top, or use flags)
# python s3_sync.py
#
# # or override everything via CLI
# python s3_sync.py \
#   --local-dir "/media/xmouy/Elements/Wellfleet backups/" \
#   --bucket neracoos-pam-data-ingest \
#   --prefix Wellfleet/ \
#   --concurrency 4 \
#   --max-workers 2 \
#   --extensions .wav .flac \
#   --dry-run   # ← test first without uploading