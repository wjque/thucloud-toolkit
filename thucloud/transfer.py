"""Reliable upload, download, and URL relay operations."""

from __future__ import annotations

import hashlib
import logging
import os
import posixpath
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import requests
from tqdm import tqdm

from .client import CloudClient, join_remote_path, normalize_remote_dir, normalize_remote_path
from .errors import is_transient_error, raise_for_status_with_body
from .links import LinkRecord
from .manifest import ManifestStore
from .multipart import build_upload_body


@dataclass(frozen=True)
class TransferOptions:
    chunk_size_bytes: int
    split_size_bytes: int
    retries: int
    retry_delay_sec: float
    replace: bool = True
    skip_existing: bool = False
    staging_mode: str = "stream"
    cache_dir: str = ".cache/thucloud"
    max_cache_bytes: int = 0
    keep_cache: bool = False
    resume: bool = True
    ensure_dirs: bool = True


@dataclass(frozen=True)
class Part:
    filename: str
    start: int
    end: int
    size: int
    ranged: bool


def retry_call(description: str, options: TransferOptions, func: Callable[[], None]) -> None:
    attempts = max(1, options.retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            func()
            return
        except Exception as exc:
            if attempt >= attempts or not is_transient_error(exc):
                raise
            delay = min(options.retry_delay_sec * (2 ** (attempt - 1)), 300.0)
            logging.warning(
                "%s failed on attempt %d/%d: %s. Retrying in %.1fs.",
                description,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)


def source_content_length(url: str) -> int:
    resp = requests.head(
        url,
        allow_redirects=True,
        headers={"Accept-Encoding": "identity"},
        timeout=30,
    )
    raise_for_status_with_body(resp, "Read source metadata {}".format(url))
    if "Content-Length" not in resp.headers:
        raise ValueError("Source URL did not provide Content-Length in HEAD: {}".format(url))
    return int(resp.headers["Content-Length"])


def validate_source_response(
    source_resp: requests.Response,
    url: str,
    expected_status: set[int],
) -> None:
    raise_for_status_with_body(source_resp, "Open source URL {}".format(url))
    if source_resp.status_code not in expected_status:
        raise ValueError(
            "Source URL returned status {}, expected {}: {}".format(
                source_resp.status_code,
                sorted(expected_status),
                url,
            )
        )
    if source_resp.headers.get("Content-Encoding") not in {None, "", "identity"}:
        raise ValueError(
            "Source URL returned Content-Encoding={}, cannot compute exact upload size: {}".format(
                source_resp.headers.get("Content-Encoding"),
                url,
            )
        )
    if "Content-Length" not in source_resp.headers:
        raise ValueError("Source URL did not provide Content-Length: {}".format(url))


def build_parts(filename: str, total_size: int, split_size: int) -> list[Part]:
    if split_size <= 0 or total_size <= split_size:
        end = max(0, total_size - 1)
        return [Part(filename=filename, start=0, end=end, size=total_size, ranged=False)]

    parts = []
    part_count = (total_size + split_size - 1) // split_size
    for part_idx in range(part_count):
        start = part_idx * split_size
        end = min(total_size - 1, start + split_size - 1)
        parts.append(
            Part(
                filename="{}.part{:03d}".format(filename, part_idx),
                start=start,
                end=end,
                size=end - start + 1,
                ranged=True,
            )
        )
    return parts


def iter_file_range(path: str, start: int, size: int, chunk_size: int) -> Iterable[bytes]:
    with open(path, "rb") as f:
        f.seek(start)
        remaining = size
        while remaining > 0:
            chunk = f.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def iter_url_part(url: str, part: Part, chunk_size: int) -> Iterable[bytes]:
    headers = {"Accept-Encoding": "identity"}
    expected_status = {200}
    if part.ranged:
        headers["Range"] = "bytes={}-{}".format(part.start, part.end)
        expected_status = {206}

    with requests.get(url, stream=True, headers=headers, timeout=(15, 60)) as source_resp:
        validate_source_response(source_resp, url, expected_status=expected_status)
        actual_size = int(source_resp.headers["Content-Length"])
        if actual_size != part.size:
            raise ValueError(
                "Source range size mismatch for {}: expected {}, got {}".format(
                    part.filename,
                    part.size,
                    actual_size,
                )
            )
        for chunk in source_resp.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk


def upload_stream_to_cloud(
    client: CloudClient,
    *,
    repo_id: str,
    remote_dir: str,
    filename: str,
    source_size: int,
    source_iter_factory: Callable[[], Iterable[bytes]],
    options: TransferOptions,
) -> None:
    remote_dir = normalize_remote_dir(remote_dir)

    def attempt() -> None:
        upload_link = client.get_upload_link(repo_id, remote_dir)
        with tqdm(
            total=source_size,
            ncols=120,
            unit="iB",
            unit_scale=True,
            unit_divisor=1024,
            desc=filename,
        ) as pbar:
            body, content_length, content_type = build_upload_body(
                source_iter=source_iter_factory(),
                source_size=source_size,
                remote_dir=remote_dir,
                filename=filename,
                replace=options.replace,
                progress=pbar.update,
            )
            headers = client.auth_headers()
            headers.update(
                {
                    "Content-Type": content_type,
                    "Content-Length": str(content_length),
                }
            )
            resp = client.session.post(upload_link, data=body, headers=headers, timeout=None)
            raise_for_status_with_body(resp, "Upload {}".format(filename))

    retry_call("Upload {}".format(filename), options, attempt)


def should_skip(existing_files: dict[str, int] | None, filename: str, expected_size: int) -> bool:
    if existing_files is None:
        return False
    current_size = existing_files.get(filename)
    if current_size is None:
        return False
    if current_size == expected_size:
        logging.info("Skipping existing remote file %s (%d bytes).", filename, expected_size)
        return True
    logging.warning(
        "Remote file %s already exists but size is %d bytes, expected %d bytes; reuploading.",
        filename,
        current_size,
        expected_size,
    )
    return False


def manifest_for(store: ManifestStore | None, *items: str) -> str | None:
    if store is None:
        return None
    return store.key(*items)


def upload_local_file(
    client: CloudClient,
    *,
    repo_id: str,
    local_path: str,
    remote_dir: str,
    options: TransferOptions,
    remote_name: str | None = None,
    manifest_store: ManifestStore | None = None,
) -> None:
    local_path = os.path.abspath(local_path)
    filename = remote_name or os.path.basename(local_path)
    total_size = os.path.getsize(local_path)
    parts = build_parts(filename, total_size, options.split_size_bytes)
    remote_dir = normalize_remote_dir(remote_dir)
    if options.ensure_dirs:
        client.ensure_dir(repo_id, remote_dir)

    if len(parts) > 1:
        logging.warning(
            "%s is %.2f GiB; uploading as %d part files of at most %.2f GiB.",
            filename,
            total_size / 1024 / 1024 / 1024,
            len(parts),
            options.split_size_bytes / 1024 / 1024 / 1024,
        )

    existing = client.remote_file_sizes(repo_id, remote_dir) if options.skip_existing else None
    manifest_id = manifest_for(manifest_store, "local", local_path, repo_id, remote_dir, filename)

    for part in parts:
        if should_skip(existing, part.filename, part.size):
            if manifest_store and manifest_id:
                manifest_store.update_part(manifest_id, part.filename, state="skipped", expected_size=part.size)
            continue

        try:
            upload_stream_to_cloud(
                client,
                repo_id=repo_id,
                remote_dir=remote_dir,
                filename=part.filename,
                source_size=part.size,
                source_iter_factory=lambda part=part: iter_file_range(
                    local_path,
                    part.start,
                    part.size,
                    options.chunk_size_bytes,
                ),
                options=options,
            )
            if manifest_store and manifest_id:
                manifest_store.update_part(manifest_id, part.filename, state="uploaded", expected_size=part.size)
            if existing is not None:
                existing[part.filename] = part.size
        except Exception as exc:
            if manifest_store and manifest_id:
                manifest_store.update_part(
                    manifest_id,
                    part.filename,
                    state="failed",
                    expected_size=part.size,
                    error=str(exc),
                )
            raise


def cache_path_for(cache_dir: str, url: str, part: Part) -> str:
    digest = hashlib.sha256("{}\0{}\0{}\0{}".format(url, part.filename, part.start, part.end).encode("utf-8")).hexdigest()
    safe_name = part.filename.replace("/", "_").replace("\\", "_")
    return os.path.join(cache_dir, "parts", "{}-{}".format(digest[:16], safe_name))


def download_url_part_to_cache(url: str, part: Part, path: str, options: TransferOptions) -> None:
    if options.max_cache_bytes and part.size > options.max_cache_bytes:
        raise ValueError(
            "{} requires {} bytes of cache, above max-cache limit {} bytes".format(
                part.filename,
                part.size,
                options.max_cache_bytes,
            )
        )

    if os.path.exists(path) and os.path.getsize(path) == part.size:
        logging.info("Reusing cached part %s.", path)
        return

    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = "{}.tmp".format(path)

    def attempt() -> None:
        downloaded = 0
        with open(tmp_path, "wb") as f:
            with tqdm(
                total=part.size,
                ncols=120,
                unit="iB",
                unit_scale=True,
                unit_divisor=1024,
                desc="cache:{}".format(part.filename),
            ) as pbar:
                for chunk in iter_url_part(url, part, options.chunk_size_bytes):
                    f.write(chunk)
                    downloaded += len(chunk)
                    pbar.update(len(chunk))
        if downloaded != part.size:
            raise ValueError(
                "Cached size mismatch for {}: expected {}, got {}".format(
                    part.filename,
                    part.size,
                    downloaded,
                )
            )
        os.replace(tmp_path, path)

    retry_call("Download cache {}".format(part.filename), options, attempt)


def upload_url(
    client: CloudClient,
    *,
    repo_id: str,
    url: str,
    filename: str,
    remote_dir: str,
    options: TransferOptions,
    manifest_store: ManifestStore | None = None,
) -> None:
    total_size = source_content_length(url)
    parts = build_parts(filename, total_size, options.split_size_bytes)
    remote_dir = normalize_remote_dir(remote_dir)
    if options.ensure_dirs:
        client.ensure_dir(repo_id, remote_dir)

    if len(parts) > 1:
        logging.warning(
            "%s is %.2f GiB; uploading as %d part files of at most %.2f GiB.",
            filename,
            total_size / 1024 / 1024 / 1024,
            len(parts),
            options.split_size_bytes / 1024 / 1024 / 1024,
        )

    existing = client.remote_file_sizes(repo_id, remote_dir) if options.skip_existing else None
    manifest_id = manifest_for(manifest_store, "url", url, repo_id, remote_dir, filename)

    for part in parts:
        if should_skip(existing, part.filename, part.size):
            if manifest_store and manifest_id:
                manifest_store.update_part(manifest_id, part.filename, state="skipped", expected_size=part.size)
            continue

        try:
            if options.staging_mode == "cache":
                cached = cache_path_for(options.cache_dir, url, part)
                download_url_part_to_cache(url, part, cached, options)
                upload_stream_to_cloud(
                    client,
                    repo_id=repo_id,
                    remote_dir=remote_dir,
                    filename=part.filename,
                    source_size=part.size,
                    source_iter_factory=lambda cached=cached, part=part: iter_file_range(
                        cached,
                        0,
                        part.size,
                        options.chunk_size_bytes,
                    ),
                    options=options,
                )
                if not options.keep_cache:
                    try:
                        os.remove(cached)
                    except FileNotFoundError:
                        pass
            else:
                upload_stream_to_cloud(
                    client,
                    repo_id=repo_id,
                    remote_dir=remote_dir,
                    filename=part.filename,
                    source_size=part.size,
                    source_iter_factory=lambda part=part: iter_url_part(
                        url,
                        part,
                        options.chunk_size_bytes,
                    ),
                    options=options,
                )

            if manifest_store and manifest_id:
                manifest_store.update_part(manifest_id, part.filename, state="uploaded", expected_size=part.size)
            if existing is not None:
                existing[part.filename] = part.size
        except Exception as exc:
            if manifest_store and manifest_id:
                manifest_store.update_part(
                    manifest_id,
                    part.filename,
                    state="failed",
                    expected_size=part.size,
                    error=str(exc),
                )
            raise


def upload_urls(
    client: CloudClient,
    *,
    repo_id: str,
    records: list[LinkRecord],
    remote_dir: str,
    options: TransferOptions,
    manifest_store: ManifestStore | None = None,
) -> list[tuple[LinkRecord, Exception]]:
    failures: list[tuple[LinkRecord, Exception]] = []
    for index, record in enumerate(records, 1):
        logging.info(
            "[%d/%d] Uploading %s to %s",
            index,
            len(records),
            record.url,
            join_remote_path(remote_dir, record.filename),
        )
        try:
            upload_url(
                client,
                repo_id=repo_id,
                url=record.url,
                filename=record.filename,
                remote_dir=remote_dir,
                options=options,
                manifest_store=manifest_store,
            )
        except Exception as exc:
            failures.append((record, exc))
            logging.error("Upload failed for %s: %s", record.url, exc)
    return failures


def upload_paths(
    client: CloudClient,
    *,
    repo_id: str,
    paths: list[str],
    remote_dir: str,
    options: TransferOptions,
    manifest_store: ManifestStore | None = None,
) -> list[tuple[str, Exception]]:
    files: list[tuple[str, str]] = []
    for input_path in paths:
        path = Path(input_path)
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    files.append((str(child), child.relative_to(path).as_posix()))
        else:
            files.append((str(path), path.name))

    failures: list[tuple[str, Exception]] = []
    for index, (path, relative_name) in enumerate(files, 1):
        target_dir = normalize_remote_dir(posixpath.join(remote_dir, posixpath.dirname(relative_name)))
        target_name = posixpath.basename(relative_name)
        logging.info("[%d/%d] Uploading %s to %s", index, len(files), path, join_remote_path(target_dir, target_name))
        try:
            upload_local_file(
                client,
                repo_id=repo_id,
                local_path=path,
                remote_dir=target_dir,
                remote_name=target_name,
                options=options,
                manifest_store=manifest_store,
            )
        except Exception as exc:
            failures.append((path, exc))
            logging.error("Upload failed for %s: %s", path, exc)
    return failures


def download_repo_file(
    client: CloudClient,
    *,
    repo_id: str,
    remote_path: str,
    output_path: str,
    options: TransferOptions,
) -> None:
    remote_path = normalize_remote_path(remote_path)
    remote_size = client.remote_file_size(repo_id, remote_path)
    output_path = os.path.abspath(output_path)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if remote_size is not None and os.path.exists(output_path) and os.path.getsize(output_path) == remote_size:
        if options.skip_existing:
            logging.info("Skipping existing local file %s (%d bytes).", output_path, remote_size)
            return

    part_path = "{}.part".format(output_path)

    def attempt() -> None:
        if options.resume and remote_size is not None and os.path.exists(part_path):
            if os.path.getsize(part_path) == remote_size:
                os.replace(part_path, output_path)
                return

        resume_from = 0
        mode = "wb"
        headers = {}
        if options.resume and os.path.exists(part_path):
            resume_from = os.path.getsize(part_path)
            if remote_size is None or resume_from < remote_size:
                headers["Range"] = "bytes={}-".format(resume_from)
                mode = "ab"
        download_link = client.get_download_link(repo_id, remote_path)
        with requests.get(download_link, stream=True, headers=headers, timeout=(15, 60)) as resp:
            raise_for_status_with_body(resp, "Download {}".format(remote_path))
            if headers and resp.status_code != 206:
                logging.warning("Server ignored Range for %s; restarting download.", remote_path)
                resume_from = 0
                mode = "wb"
            total = remote_size
            initial = resume_from if mode == "ab" else 0
            with open(part_path, mode) as f:
                with tqdm(
                    total=total,
                    initial=initial,
                    ncols=120,
                    unit="iB",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=os.path.basename(output_path),
                ) as pbar:
                    for chunk in resp.iter_content(chunk_size=options.chunk_size_bytes):
                        if chunk:
                            f.write(chunk)
                            pbar.update(len(chunk))

        if remote_size is not None and os.path.getsize(part_path) != remote_size:
            raise ValueError(
                "Downloaded size mismatch for {}: expected {}, got {}".format(
                    remote_path,
                    remote_size,
                    os.path.getsize(part_path),
                )
            )
        os.replace(part_path, output_path)

    retry_call("Download {}".format(remote_path), options, attempt)
