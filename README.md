# thucloud

Reliable command line tools and Python helpers for Tsinghua Cloud operations.

## Documentation

- [中文](doc/README_cn.md)
- [English](README.md)

The project focuses on large-file workflows:

- download large files from a Tsinghua Cloud library or share link;
- upload large local files or directories to a library;
- relay external dataset URLs into a library through the local machine;
- split oversized uploads into resumable `.partNNN` files;
- retry transient server and network failures safely.

Naming:

- distribution package: `thucloud-toolkit`
- Python import package: `thucloud`
- command line executable: `thucloud`

## Install

For normal use, install the package from this repository:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

After installation, use the `thucloud` command directly:

```bash
thucloud --help
```

For source-tree development, `python3 -m thucloud --help` also works.

## Authentication

Prefer an environment variable so the token does not enter shell history:

```bash
export THUCLOUD_TOKEN=<your_web_api_auth_token>
```

You can list libraries with:

```bash
thucloud repos
```

If a token was pasted into chat, logs, or scripts, rotate it in the web UI.

## Common Commands

List a remote directory:

```bash
thucloud ls \
  --repo-id <library-id> \
  --remote-dir /behave
```

Upload local files or directories:

```bash
thucloud upload \
  --repo-id <library-id> \
  --remote-dir /datasets/behave \
  ./Date03.zip
```

Relay URLs from a text file into cloud storage:

```bash
thucloud relay \
  --repo-id <library-id> \
  --remote-dir /datasets/behave \
  --links-file deprecated/links.txt \
  --split-size-gb 1 \
  --staging-mode stream
```

Use local staged parts when the network is unstable:

```bash
thucloud relay \
  --repo-id <library-id> \
  --remote-dir /datasets/behave \
  --links-file deprecated/links.txt \
  --split-size-gb 1 \
  --staging-mode cache \
  --cache-dir .cache/thucloud \
  --max-cache-gb 2
```

Download from a library:

```bash
thucloud download \
  --repo-id <library-id> \
  -o downloads \
  /datasets/behave/Date03.zip.part000
```

Download from a share link:

```bash
thucloud share-download \
  --share-url https://cloud.tsinghua.edu.cn/d/<share-key>/ \
  --include "*.zip" \
  -o downloads \
  -y
```

## Large File Behavior

Uploads larger than `--split-size-gb` are stored as independent part files:

```text
Date03.zip.part000
Date03.zip.part001
Date03.zip.part002
```

To reconstruct after downloading all parts:

```bash
cat Date03.zip.part* > Date03.zip
```

Defaults are chosen for reliability:

- `--split-size-gb 1`
- `--retries 5`
- `--skip-existing`
- `--resume`
- `--verify-upload`
- `--upload-timeout-sec 600`

The `relay` command cannot make Tsinghua Cloud fetch third-party URLs server-side. It uses the local machine as the transfer client. In `stream` mode data flows through memory; in `cache` mode each part is downloaded to `.cache/thucloud/parts`, uploaded, then removed unless `--keep-cache` is set.

For stricter local uploads, `--checksum-source` hashes each local part before and after upload. If the source file changes during transfer, the tool stops and records the affected manifest as failed so a later run will reupload instead of trusting a same-size remote file.

For cache-mode relays, stale `*.tmp` cache files are cleaned before the run by default. Use `--no-cleanup-cache`, `--cache-ttl-hours`, or `--keep-cache` to adjust that behavior.

## Error Notes

- `413 Request Entity Too Large`: the upload request is too large. Lower `--split-size-gb`.
- `403 Permission denied`: token or library permission is wrong, or the upload link was created for the wrong directory.
- `403 Access token not found`: the temporary upload endpoint expired or was dropped. The tool retries with a fresh upload link.
- `500 Internal error`: cloud backend failed while accepting a part. Retry or lower `--split-size-gb`.
- `SSL unexpected eof`: the TLS connection closed mid-transfer. Retry or use `--staging-mode cache`.
