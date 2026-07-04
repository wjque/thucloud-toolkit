"""Download files from Tsinghua Cloud share links."""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import urllib.parse

import requests
from tqdm import tqdm

from .config import DEFAULT_CLOUD_URL
from .errors import raise_for_status_with_body
from .transfer import TransferOptions, retry_call


def get_share_key(url: str, cloud_url: str = DEFAULT_CLOUD_URL) -> str:
    prefix = "{}/d/".format(cloud_url.rstrip("/"))
    if not url.startswith(prefix):
        raise ValueError("Share link should start with {}".format(prefix))
    return url[len(prefix):].replace("/", "")


def verify_password(session: requests.Session, cloud_url: str, share_key: str, password: str | None = None) -> None:
    share_url = "{}/d/{}/".format(cloud_url.rstrip("/"), share_key)
    resp = session.get(share_url)
    raise_for_status_with_body(resp, "Open share link")
    match = re.findall(r'<input type="hidden" name="csrfmiddlewaretoken" value="(.*)">', resp.text)
    if not match:
        return

    password = password or input("Please enter the share password: ")
    token = match[0]
    resp = session.post(
        share_url,
        data={"csrfmiddlewaretoken": token, "token": share_key, "password": password},
        headers={"Referer": share_url},
    )
    raise_for_status_with_body(resp, "Verify share password")
    if "Please enter a correct password" in resp.text:
        raise ValueError("Wrong share password.")


def get_root_dir(session: requests.Session, cloud_url: str, share_key: str) -> str:
    resp = session.get("{}/d/{}/".format(cloud_url.rstrip("/"), share_key))
    raise_for_status_with_body(resp, "Read share root")
    root_dir = re.findall(r'<meta property="og:title" content="(.*)" />', resp.text)
    if not root_dir:
        return share_key
    return root_dir[0]


def is_match(file_path: str, pattern: str | None) -> bool:
    file_path = file_path.lstrip("/")
    return pattern is None or fnmatch.fnmatch(file_path, pattern)


def list_share_files(
    session: requests.Session,
    cloud_url: str,
    share_key: str,
    *,
    path: str = "/",
    pattern: str | None = None,
) -> list[dict]:
    encoded_path = urllib.parse.quote(path)
    resp = session.get(
        "{}/api/v2.1/share-links/{}/dirents/?path={}".format(
            cloud_url.rstrip("/"),
            share_key,
            encoded_path,
        )
    )
    raise_for_status_with_body(resp, "List share directory {}".format(path))
    objects = resp.json()["dirent_list"]
    filelist = []
    for obj in objects:
        if obj["is_dir"]:
            filelist.extend(
                list_share_files(
                    session,
                    cloud_url,
                    share_key,
                    path=obj["folder_path"],
                    pattern=pattern,
                )
            )
        elif is_match(obj["file_path"], pattern):
            filelist.append(obj)
    return filelist


def print_filelist(filelist: list[dict]) -> None:
    print("=" * 100)
    print("Last Modified Time".ljust(25), " ", "File Size".rjust(12), " ", "File Path")
    print("-" * 100)
    for index, file in enumerate(filelist, 1):
        print(file["last_modified"], " ", str(file["size"]).rjust(12), " ", file["file_path"])
        if index == 100 and len(filelist) > 100:
            print("... {} more files".format(len(filelist) - 100))
            break
    print("-" * 100)


def download_share_file(
    session: requests.Session,
    cloud_url: str,
    share_key: str,
    file_info: dict,
    output_path: str,
    options: TransferOptions,
) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    part_path = "{}.part".format(output_path)
    expected_size = int(file_info["size"])

    if options.skip_existing and os.path.exists(output_path) and os.path.getsize(output_path) == expected_size:
        logging.info("Skipping existing local file %s.", output_path)
        return

    def attempt() -> None:
        if options.resume and os.path.exists(part_path) and os.path.getsize(part_path) == expected_size:
            os.replace(part_path, output_path)
            return

        resume_from = 0
        headers = {}
        mode = "wb"
        if options.resume and os.path.exists(part_path):
            resume_from = os.path.getsize(part_path)
            if resume_from < expected_size:
                headers["Range"] = "bytes={}-".format(resume_from)
                mode = "ab"

        resp = session.get(
            "{}/d/{}/files/".format(cloud_url.rstrip("/"), share_key),
            params={"p": file_info["file_path"], "dl": "1"},
            headers=headers,
            stream=True,
            timeout=(15, 60),
        )
        raise_for_status_with_body(resp, "Download share file {}".format(file_info["file_path"]))
        if headers and resp.status_code != 206:
            logging.warning("Server ignored Range for %s; restarting download.", file_info["file_path"])
            resume_from = 0
            mode = "wb"

        with open(part_path, mode) as f:
            with tqdm(
                total=expected_size,
                initial=resume_from if mode == "ab" else 0,
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

        if os.path.getsize(part_path) != expected_size:
            raise ValueError(
                "Downloaded size mismatch for {}: expected {}, got {}".format(
                    file_info["file_path"],
                    expected_size,
                    os.path.getsize(part_path),
                )
            )
        os.replace(part_path, output_path)

    retry_call("Download share file {}".format(file_info["file_path"]), options, attempt)


def download_share(
    *,
    share_url: str,
    output_dir: str,
    include: str | None,
    password: str | None,
    yes: bool,
    cloud_url: str,
    options: TransferOptions,
) -> None:
    session = requests.Session()
    share_key = get_share_key(share_url, cloud_url)
    logging.info("Share key: %s", share_key)
    verify_password(session, cloud_url, share_key, password=password)
    filelist = list_share_files(session, cloud_url, share_key, pattern=include)
    filelist.sort(key=lambda x: x["file_path"])
    if not filelist:
        logging.info("No file found.")
        return

    print_filelist(filelist)
    total_size = sum(int(file["size"]) for file in filelist)
    logging.info("# Files: %d. Total size: %.2f GiB.", len(filelist), total_size / 1024 / 1024 / 1024)
    if not yes:
        answer = input("Start downloading? [y/N] ")
        if answer.lower() != "y":
            return

    root_dir = get_root_dir(session, cloud_url, share_key)
    save_root = os.path.join(os.path.abspath(output_dir), root_dir)
    for index, file_info in enumerate(filelist, 1):
        save_path = os.path.join(save_root, file_info["file_path"].lstrip("/"))
        logging.info("[%d/%d] Downloading %s", index, len(filelist), file_info["file_path"])
        download_share_file(session, cloud_url, share_key, file_info, save_path, options)
