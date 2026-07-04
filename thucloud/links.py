"""Parse URL lists used by upload-urls."""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass


URL_PATTERN = re.compile(r"https?://[^\s<>\"]+")


@dataclass(frozen=True)
class LinkRecord:
    line_no: int
    label: str
    url: str
    filename: str


def strip_url_suffix(url: str) -> str:
    return url.rstrip(").,;")


def filename_from_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    name = os.path.basename(urllib.parse.unquote(parsed.path.rstrip("/")))
    if not name:
        name = parsed.netloc.replace(":", "_") or "download"
    name = re.sub(r"[\x00-\x1f/\\]+", "_", name).strip()
    return name or "download"


def unique_filename(filename: str, used: dict[str, int]) -> str:
    if filename not in used:
        used[filename] = 1
        return filename
    used[filename] += 1
    stem, ext = os.path.splitext(filename)
    return "{}_{}{}".format(stem, used[filename], ext)


def parse_links_file(path: str) -> list[LinkRecord]:
    records: list[LinkRecord] = []
    used: dict[str, int] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            match = URL_PATTERN.search(line)
            if not match:
                continue
            url = strip_url_suffix(match.group(0))
            filename = unique_filename(filename_from_url(url), used)
            label = line[:match.start()].strip().rstrip(":")
            records.append(LinkRecord(line_no=line_no, label=label, url=url, filename=filename))
    return records

