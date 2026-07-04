"""Command line interface for thucloud."""

from __future__ import annotations

import argparse
import logging
import os

from .client import CloudClient, normalize_remote_dir, normalize_remote_path
from .config import (
    DEFAULT_CACHE_DIR,
    DEFAULT_CHUNK_SIZE_MB,
    DEFAULT_CLOUD_URL,
    DEFAULT_MAX_CACHE_GB,
    DEFAULT_RETRIES,
    DEFAULT_RETRY_DELAY_SEC,
    DEFAULT_SPLIT_SIZE_GB,
)
from .links import parse_links_file
from .manifest import ManifestStore
from .share import download_share
from .transfer import (
    TransferOptions,
    download_repo_file,
    upload_paths,
    upload_urls,
)


def positive_bytes_from_gb(value: float) -> int:
    return int(max(0.0, value) * 1024 * 1024 * 1024)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def add_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cloud-url", default=DEFAULT_CLOUD_URL, help="Tsinghua Cloud base URL")
    parser.add_argument(
        "--token",
        default=os.environ.get("THUCLOUD_TOKEN"),
        help="API token. Prefer THUCLOUD_TOKEN instead of putting secrets in shell history",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("THUCLOUD_USERNAME"),
        help="Account username. Used only when --token is not provided",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("THUCLOUD_PASSWORD"),
        help="Account password. Used only when --token is not provided",
    )


def add_common_transfer_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--chunk-size-mb", type=int, default=DEFAULT_CHUNK_SIZE_MB, help="I/O chunk size in MiB")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="Retries per file or part")
    parser.add_argument(
        "--retry-delay-sec",
        type=float,
        default=DEFAULT_RETRY_DELAY_SEC,
        help="Initial retry delay; later retries use exponential backoff",
    )
    parser.add_argument(
        "--skip-existing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip local or remote files that already exist with the expected size",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resume partial local downloads and keep transfer manifests",
    )


def add_upload_transfer_args(parser: argparse.ArgumentParser) -> None:
    add_common_transfer_args(parser)
    parser.add_argument(
        "--split-size-gb",
        type=float,
        default=DEFAULT_SPLIT_SIZE_GB,
        help="Upload files larger than this as .partNNN files. Use 0 to disable",
    )
    parser.add_argument(
        "--replace",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Ask cloud storage to replace existing files on upload",
    )


def add_cache_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--staging-mode",
        choices=("stream", "cache"),
        default="stream",
        help="stream pipes URL data directly through memory; cache downloads each part before uploading",
    )
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR, help="Manifest and temporary part cache directory")
    parser.add_argument(
        "--max-cache-gb",
        type=float,
        default=DEFAULT_MAX_CACHE_GB,
        help="Maximum cache required by one staged part. Use 0 for no explicit limit",
    )
    parser.add_argument("--keep-cache", action="store_true", help="Keep staged URL parts after upload")


def make_client(args: argparse.Namespace) -> CloudClient:
    return CloudClient(
        cloud_url=args.cloud_url,
        token=args.token,
        username=args.username,
        password=args.password,
    )


def make_options(args: argparse.Namespace, *, ensure_dirs: bool = True) -> TransferOptions:
    return TransferOptions(
        chunk_size_bytes=max(1, args.chunk_size_mb) * 1024 * 1024,
        split_size_bytes=positive_bytes_from_gb(getattr(args, "split_size_gb", 0.0)),
        retries=max(0, args.retries),
        retry_delay_sec=max(0.0, args.retry_delay_sec),
        replace=getattr(args, "replace", True),
        skip_existing=args.skip_existing,
        staging_mode=getattr(args, "staging_mode", "stream"),
        cache_dir=getattr(args, "cache_dir", DEFAULT_CACHE_DIR),
        max_cache_bytes=positive_bytes_from_gb(getattr(args, "max_cache_gb", 0.0)),
        keep_cache=getattr(args, "keep_cache", False),
        resume=args.resume,
        ensure_dirs=ensure_dirs,
    )


def make_manifest_store(args: argparse.Namespace) -> ManifestStore | None:
    if not getattr(args, "resume", True):
        return None
    return ManifestStore(getattr(args, "cache_dir", DEFAULT_CACHE_DIR))


def command_auth_token(args: argparse.Namespace) -> int:
    client = make_client(args)
    print(client.token)
    return 0


def command_repos(args: argparse.Namespace) -> int:
    client = make_client(args)
    repos = client.list_repos()
    print("Repo ID".ljust(40), "Name")
    print("-" * 80)
    for repo in repos:
        repo_id = repo.get("id") or repo.get("repo_id", "")
        name = repo.get("name", "")
        permission = repo.get("permission", "")
        print(str(repo_id).ljust(40), "{} {}".format(name, "({})".format(permission) if permission else ""))
    return 0


def command_ls(args: argparse.Namespace) -> int:
    client = make_client(args)
    entries = client.list_dir(args.repo_id, args.remote_dir)
    print("Type".ljust(8), "Size".rjust(12), "Name")
    print("-" * 80)
    for entry in entries:
        name = entry.get("name", "")
        entry_type = entry.get("type") or ("dir" if entry.get("is_dir") else "file")
        size = entry.get("size", entry.get("file_size", ""))
        print(str(entry_type).ljust(8), str(size).rjust(12), name)
    return 0


def command_mkdir(args: argparse.Namespace) -> int:
    client = make_client(args)
    client.ensure_repo_access(args.repo_id)
    client.ensure_dir(args.repo_id, args.remote_dir)
    logging.info("Remote directory ready: %s", normalize_remote_dir(args.remote_dir))
    return 0


def command_upload(args: argparse.Namespace) -> int:
    client = make_client(args)
    client.ensure_repo_access(args.repo_id)
    options = make_options(args, ensure_dirs=not args.no_mkdir)
    store = make_manifest_store(args)
    failures = upload_paths(
        client,
        repo_id=args.repo_id,
        paths=args.paths,
        remote_dir=args.remote_dir,
        options=options,
        manifest_store=store,
    )
    if failures:
        logging.error("%d path(s) failed.", len(failures))
        return 1
    logging.info("Upload finished.")
    return 0


def command_relay(args: argparse.Namespace) -> int:
    records = parse_links_file(args.links_file)
    if not records:
        logging.info("No URL found in %s.", args.links_file)
        return 0

    print("Files parsed from {}:".format(args.links_file))
    for index, record in enumerate(records, 1):
        print("[{}] line {} -> {} ({})".format(index, record.line_no, record.filename, record.url))
    if args.dry_run:
        return 0

    client = make_client(args)
    client.ensure_repo_access(args.repo_id)
    options = make_options(args, ensure_dirs=not args.no_mkdir)
    store = make_manifest_store(args)
    failures = upload_urls(
        client,
        repo_id=args.repo_id,
        records=records,
        remote_dir=args.remote_dir,
        options=options,
        manifest_store=store,
    )
    if failures:
        logging.error("%d URL(s) failed.", len(failures))
        return 1
    logging.info("URL relay finished.")
    return 0


def command_download(args: argparse.Namespace) -> int:
    client = make_client(args)
    client.ensure_repo_access(args.repo_id)
    options = make_options(args)
    output = args.output

    failures = []
    for remote_path in args.remote_paths:
        try:
            normalized = normalize_remote_path(remote_path)
            if len(args.remote_paths) == 1 and output and not os.path.isdir(output):
                output_path = output
            else:
                output_dir = output or "."
                output_path = os.path.join(output_dir, normalized.lstrip("/"))
            logging.info("Downloading %s to %s", normalized, output_path)
            download_repo_file(
                client,
                repo_id=args.repo_id,
                remote_path=normalized,
                output_path=output_path,
                options=options,
            )
        except Exception as exc:
            failures.append((remote_path, exc))
            logging.error("Download failed for %s: %s", remote_path, exc)
    if failures:
        logging.error("%d file(s) failed.", len(failures))
        return 1
    logging.info("Download finished.")
    return 0


def command_share_download(args: argparse.Namespace) -> int:
    options = make_options(args)
    download_share(
        share_url=args.share_url,
        output_dir=args.output_dir,
        include=args.include,
        password=args.share_password,
        yes=args.yes,
        cloud_url=args.cloud_url,
        options=options,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="thucloud",
        description="Reliable Tsinghua Cloud operations for large files and URL datasets.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Logging verbosity",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth-token", help="Login and print an API token")
    add_auth_args(auth)
    auth.set_defaults(func=command_auth_token)

    repos = subparsers.add_parser("repos", help="List visible libraries")
    add_auth_args(repos)
    repos.set_defaults(func=command_repos)

    ls = subparsers.add_parser("ls", help="List a remote directory")
    add_auth_args(ls)
    ls.add_argument("--repo-id", required=True)
    ls.add_argument("--remote-dir", default="/")
    ls.set_defaults(func=command_ls)

    mkdir = subparsers.add_parser("mkdir", help="Create a remote directory if needed")
    add_auth_args(mkdir)
    mkdir.add_argument("--repo-id", required=True)
    mkdir.add_argument("--remote-dir", required=True)
    mkdir.set_defaults(func=command_mkdir)

    upload = subparsers.add_parser("upload", help="Upload local files or directories")
    add_auth_args(upload)
    add_upload_transfer_args(upload)
    upload.add_argument("--repo-id", required=True)
    upload.add_argument("--remote-dir", default="/")
    upload.add_argument("--no-mkdir", action="store_true", help="Do not create remote directories")
    upload.add_argument("paths", nargs="+", help="Local files or directories")
    upload.set_defaults(func=command_upload)

    relay = subparsers.add_parser("relay", help="Relay external URLs into a cloud library")
    add_auth_args(relay)
    add_upload_transfer_args(relay)
    add_cache_args(relay)
    relay.add_argument("--repo-id", required=True)
    relay.add_argument("--remote-dir", default="/")
    relay.add_argument("--links-file", required=True, help="Text file containing source URLs")
    relay.add_argument("--no-mkdir", action="store_true", help="Do not create remote directories")
    relay.add_argument("--dry-run", action="store_true", help="Parse links without uploading")
    relay.set_defaults(func=command_relay)

    download = subparsers.add_parser("download", help="Download files from a cloud library")
    add_auth_args(download)
    add_common_transfer_args(download)
    download.add_argument("--repo-id", required=True)
    download.add_argument("-o", "--output", help="Output file for one remote path, or output directory for many")
    download.add_argument("remote_paths", nargs="+", help="Remote file path(s), for example /behave/file.zip")
    download.set_defaults(func=command_download)

    share_download = subparsers.add_parser("share-download", help="Download files from a Tsinghua Cloud share link")
    share_download.add_argument("--cloud-url", default=DEFAULT_CLOUD_URL)
    add_common_transfer_args(share_download)
    share_download.add_argument("--share-url", required=True)
    share_download.add_argument("-o", "--output-dir", default="downloads")
    share_download.add_argument("--include", help="Glob filter, for example '*.zip'")
    share_download.add_argument("--share-password", help="Password for protected share links")
    share_download.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    share_download.set_defaults(func=command_share_download)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.log_level)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
