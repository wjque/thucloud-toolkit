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
from .errors import SourceChangedError, TransferVerificationError, is_transient_error, raise_for_status_with_body
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
    connect_timeout_sec: float = 15.0
    read_timeout_sec: float = 60.0
    upload_timeout_sec: float = 600.0
    verify_upload: bool = True
    cleanup_cache: bool = True
    cache_ttl_sec: float = 24 * 60 * 60
    checksum_source: bool = False


@dataclass(frozen=True)
class Part:
    filename: str
    start: int
    end: int
    size: int
    ranged: bool


@dataclass(frozen=True)
class SourceSnapshot:
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int | None = None


def request_timeout(connect_timeout_sec: float, read_timeout_sec: float) -> tuple[float | None, float | None] | None:
    connect_timeout = connect_timeout_sec if connect_timeout_sec > 0 else None
    read_timeout = read_timeout_sec if read_timeout_sec > 0 else None
    if connect_timeout is None and read_timeout is None:
        return None
    return (connect_timeout, read_timeout)


def source_request_timeout(options: TransferOptions) -> tuple[float | None, float | None] | None:
    return request_timeout(options.connect_timeout_sec, options.read_timeout_sec)


def upload_request_timeout(options: TransferOptions) -> tuple[float | None, float | None] | None:
    return request_timeout(options.connect_timeout_sec, options.upload_timeout_sec)


def snapshot_source_file(path: str) -> SourceSnapshot:
    stat = os.stat(path)
    return SourceSnapshot(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def assert_source_unchanged(path: str, snapshot: SourceSnapshot) -> None:
    current = snapshot_source_file(path)
    if current != snapshot:
        raise SourceChangedError(
            "Source file changed during upload: {} "
            "(was size={}, mtime_ns={}; now size={}, mtime_ns={})".format(
                path,
                snapshot.size,
                snapshot.mtime_ns,
                current.size,
                current.mtime_ns,
            )
        )


def file_range_sha256(path: str, start: int, size: int, chunk_size: int) -> str:
    digest = hashlib.sha256()
    remaining = size
    with open(path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            chunk = f.read(min(chunk_size, remaining))
            if not chunk:
                raise SourceChangedError(
                    "File ended before expected range was read: {} at offset {}".format(path, start + size - remaining)
                )
            remaining -= len(chunk)
            digest.update(chunk)
    return digest.hexdigest()


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


def source_content_length(url: str, options: TransferOptions) -> int:
    resp = requests.head(
        url,
        allow_redirects=True,
        headers={"Accept-Encoding": "identity"},
        timeout=source_request_timeout(options),
    )
    try:
        raise_for_status_with_body(resp, "Read source metadata {}".format(url))
        if "Content-Length" not in resp.headers:
            raise ValueError("Source URL did not provide Content-Length in HEAD: {}".format(url))
        return int(resp.headers["Content-Length"])
    finally:
        resp.close()


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
                raise SourceChangedError(
                    "File ended before expected range was read: {} at offset {}".format(path, start + size - remaining)
                )
            remaining -= len(chunk)
            yield chunk


def iter_url_part(url: str, part: Part, chunk_size: int, options: TransferOptions) -> Iterable[bytes]:
    headers = {"Accept-Encoding": "identity"}
    expected_status = {200}
    if part.ranged:
        headers["Range"] = "bytes={}-{}".format(part.start, part.end)
        expected_status = {206}

    with requests.get(url, stream=True, headers=headers, timeout=source_request_timeout(options)) as source_resp:
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


def verify_remote_file_size(
    client: CloudClient,
    *,
    repo_id: str,
    remote_dir: str,
    filename: str,
    expected_size: int,
) -> None:
    remote_path = join_remote_path(remote_dir, filename)
    actual_size = client.remote_file_size(repo_id, remote_path)
    if actual_size != expected_size:
        observed = "missing" if actual_size is None else str(actual_size)
        raise TransferVerificationError(
            "Uploaded file size verification failed for {}: expected {}, got {}".format(
                remote_path,
                expected_size,
                observed,
            )
        )


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
            resp = client.session.post(upload_link, data=body, headers=headers, timeout=upload_request_timeout(options))
            try:
                raise_for_status_with_body(resp, "Upload {}".format(filename))
            finally:
                resp.close()
    retry_call("Upload {}".format(filename), options, attempt)
    if options.verify_upload:
        retry_call(
            "Verify upload {}".format(filename),
            options,
            lambda: verify_remote_file_size(
                client,
                repo_id=repo_id,
                remote_dir=remote_dir,
                filename=filename,
                expected_size=source_size,
            ),
        )


def should_skip(
    existing_files: dict[str, int] | None,
    filename: str,
    expected_size: int,
    *,
    manifest_store: ManifestStore | None = None,
    manifest_id: str | None = None,
    require_checksum: bool = False,
    source_sha256: str | None = None,
) -> bool:
    if existing_files is None:
        return False
    current_size = existing_files.get(filename)
    if current_size is None:
        return False
    if current_size != expected_size:
        logging.warning(
            "Remote file %s already exists but size is %d bytes, expected %d bytes; reuploading.",
            filename,
            current_size,
            expected_size,
        )
        return False

    if manifest_store and manifest_id:
        part_info = manifest_store.part_info(manifest_id, filename) or {}
        state = part_info.get("state")
        if state in {"failed", "interrupted"}:
            logging.warning(
                "Remote file %s matches expected size, but previous manifest state is %s; reuploading.",
                filename,
                state,
            )
            return False
        if require_checksum and part_info.get("source_sha256") != source_sha256:
            logging.warning(
                "Remote file %s matches expected size, but source checksum changed or is unknown; reuploading.",
                filename,
            )
            return False
    logging.info("Skipping existing remote file %s (%d bytes).", filename, expected_size)
    return True


def manifest_for(store: ManifestStore | None, *items: str) -> str | None:
    if store is None:
        return None
    return store.key(*items)


def mark_manifest_parts(
    manifest_store: ManifestStore | None,
    manifest_id: str | None,
    parts: list[Part],
    *,
    state: str,
    error: str,
) -> None:
    if not manifest_store or not manifest_id:
        return
    for item in parts:
        manifest_store.update_part(
            manifest_id,
            item.filename,
            state=state,
            expected_size=item.size,
            error=error,
        )


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
    source_snapshot = snapshot_source_file(local_path)
    total_size = source_snapshot.size
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
        try:
            assert_source_unchanged(local_path, source_snapshot)
            source_sha256 = (
                file_range_sha256(local_path, part.start, part.size, options.chunk_size_bytes)
                if options.checksum_source
                else None
            )
            if should_skip(
                existing,
                part.filename,
                part.size,
                manifest_store=manifest_store,
                manifest_id=manifest_id,
                require_checksum=options.checksum_source,
                source_sha256=source_sha256,
            ):
                if manifest_store and manifest_id:
                    manifest_store.update_part(
                        manifest_id,
                        part.filename,
                        state="skipped",
                        expected_size=part.size,
                        source_sha256=source_sha256,
                    )
                continue

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
            if options.checksum_source:
                current_sha256 = file_range_sha256(local_path, part.start, part.size, options.chunk_size_bytes)
                if current_sha256 != source_sha256:
                    raise SourceChangedError(
                        "Source file changed while uploading {}: part {} checksum changed".format(
                            local_path,
                            part.filename,
                        )
                    )
            assert_source_unchanged(local_path, source_snapshot)
            if manifest_store and manifest_id:
                manifest_store.update_part(
                    manifest_id,
                    part.filename,
                    state="uploaded",
                    expected_size=part.size,
                    source_sha256=source_sha256,
                )
            if existing is not None:
                existing[part.filename] = part.size
        except KeyboardInterrupt:
            if manifest_store and manifest_id:
                manifest_store.update_part(
                    manifest_id,
                    part.filename,
                    state="interrupted",
                    expected_size=part.size,
                    error="Interrupted by user",
                )
            raise
        except SourceChangedError as exc:
            mark_manifest_parts(manifest_store, manifest_id, parts, state="failed", error=str(exc))
            raise
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


def cleanup_transfer_cache(cache_dir: str, ttl_seconds: float) -> int:
    parts_dir = os.path.join(cache_dir, "parts")
    if not os.path.isdir(parts_dir):
        return 0

    removed = 0
    cutoff = time.time() - max(0.0, ttl_seconds)
    for root, _dirs, files in os.walk(parts_dir):
        for filename in files:
            if not filename.endswith(".tmp"):
                continue
            path = os.path.join(root, filename)
            try:
                if ttl_seconds > 0 and os.path.getmtime(path) > cutoff:
                    continue
                os.remove(path)
                removed += 1
                logging.info("Removed stale cache temp file %s.", path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                logging.warning("Could not remove stale cache temp file %s: %s", path, exc)
    return removed


def remove_cache_file(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        logging.warning("Could not remove cache file %s: %s", path, exc)


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
                for chunk in iter_url_part(url, part, options.chunk_size_bytes, options):
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
    total_size = source_content_length(url, options)
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
        try:
            if should_skip(existing, part.filename, part.size, manifest_store=manifest_store, manifest_id=manifest_id):
                if options.staging_mode == "cache" and not options.keep_cache:
                    remove_cache_file(cache_path_for(options.cache_dir, url, part))
                if manifest_store and manifest_id:
                    manifest_store.update_part(manifest_id, part.filename, state="skipped", expected_size=part.size)
                continue

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
                    remove_cache_file(cached)
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
                        options,
                    ),
                    options=options,
                )

            if manifest_store and manifest_id:
                manifest_store.update_part(manifest_id, part.filename, state="uploaded", expected_size=part.size)
            if existing is not None:
                existing[part.filename] = part.size
        except KeyboardInterrupt:
            if manifest_store and manifest_id:
                manifest_store.update_part(
                    manifest_id,
                    part.filename,
                    state="interrupted",
                    expected_size=part.size,
                    error="Interrupted by user",
                )
            raise
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
    if options.staging_mode == "cache" and options.cleanup_cache:
        cleanup_transfer_cache(options.cache_dir, options.cache_ttl_sec)

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


def remote_entry_name(entry: dict) -> str | None:
    value = entry.get("name") or entry.get("file_name") or entry.get("folder_name")
    if value:
        return str(value)
    path = entry.get("path") or entry.get("file_path") or entry.get("folder_path")
    if not path:
        return None
    name = posixpath.basename(str(path).rstrip("/"))
    return name or None


def remote_entry_is_dir(entry: dict) -> bool:
    entry_type = entry.get("type")
    return entry_type == "dir" or bool(entry.get("is_dir"))


def remote_entry_file_size(entry: dict) -> int | None:
    size = entry.get("size", entry.get("file_size"))
    if size is None:
        return None
    try:
        return int(size)
    except (TypeError, ValueError):
        return None


def list_repo_files_recursive(
    client: CloudClient,
    *,
    repo_id: str,
    remote_dir: str,
    output_dir: str | None = None,
) -> list[RemoteFile]:
    remote_dir = normalize_remote_dir(remote_dir)
    files: list[RemoteFile] = []

    if output_dir is not None:
        os.makedirs(os.path.join(os.path.abspath(output_dir), remote_dir.lstrip("/")), exist_ok=True)

    for entry in client.list_dir(repo_id, remote_dir):
        if not isinstance(entry, dict):
            continue
        name = remote_entry_name(entry)
        if not name:
            continue
        remote_path = join_remote_path(remote_dir, name)
        if remote_entry_is_dir(entry):
            files.extend(
                list_repo_files_recursive(
                    client,
                    repo_id=repo_id,
                    remote_dir=remote_path,
                    output_dir=output_dir,
                )
            )
        else:
            files.append(RemoteFile(path=normalize_remote_path(remote_path), size=remote_entry_file_size(entry)))
    return files


def download_repo_dir(
    client: CloudClient,
    *,
    repo_id: str,
    remote_dir: str,
    output_dir: str,
    options: TransferOptions,
) -> list[tuple[str, Exception]]:
    remote_dir = normalize_remote_dir(remote_dir)
    output_dir = os.path.abspath(output_dir)
    files = list_repo_files_recursive(client, repo_id=repo_id, remote_dir=remote_dir, output_dir=output_dir)
    logging.info("Found %d file(s) under %s.", len(files), remote_dir)

    failures: list[tuple[str, Exception]] = []
    for index, remote_file in enumerate(files, 1):
        output_path = os.path.join(output_dir, remote_file.path.lstrip("/"))
        logging.info("[%d/%d] Downloading %s to %s", index, len(files), remote_file.path, output_path)
        try:
            download_repo_file(
                client,
                repo_id=repo_id,
                remote_path=remote_file.path,
                output_path=output_path,
                options=options,
            )
        except Exception as exc:
            failures.append((remote_file.path, exc))
            logging.error("Download failed for %s: %s", remote_file.path, exc)
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
        with requests.get(download_link, stream=True, headers=headers, timeout=source_request_timeout(options)) as resp:
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
