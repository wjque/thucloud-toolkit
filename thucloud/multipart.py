"""Streaming multipart/form-data body generation."""

from __future__ import annotations

import mimetypes
import uuid
from collections.abc import Iterable
from typing import Callable


def escape_multipart_header_value(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', r'\"')


def multipart_field(boundary: str, name: str, value: str) -> bytes:
    return (
        "--{}\r\n"
        "Content-Disposition: form-data; name=\"{}\"\r\n"
        "\r\n"
        "{}\r\n"
    ).format(boundary, escape_multipart_header_value(name), value).encode("utf-8")


def multipart_file_header(boundary: str, field_name: str, filename: str, content_type: str) -> bytes:
    return (
        "--{}\r\n"
        "Content-Disposition: form-data; name=\"{}\"; filename=\"{}\"\r\n"
        "Content-Type: {}\r\n"
        "\r\n"
    ).format(
        boundary,
        escape_multipart_header_value(field_name),
        escape_multipart_header_value(filename),
        content_type,
    ).encode("utf-8")


class StreamingMultipartBody:
    def __init__(self, iterable: Iterable[bytes], content_length: int):
        self.iterable = iterable
        self.content_length = content_length

    def __iter__(self):
        return iter(self.iterable)

    def __len__(self):
        return self.content_length


def build_upload_body(
    *,
    source_iter: Iterable[bytes],
    source_size: int,
    remote_dir: str,
    filename: str,
    replace: bool,
    progress: Callable[[int], None] | None = None,
) -> tuple[StreamingMultipartBody, int, str]:
    boundary = "----thucloud-{}".format(uuid.uuid4().hex)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    fields = [
        multipart_field(boundary, "parent_dir", remote_dir),
        multipart_field(boundary, "replace", "1" if replace else "0"),
    ]
    file_header = multipart_file_header(boundary, "file", filename, content_type)
    closing = "\r\n--{}--\r\n".format(boundary).encode("utf-8")
    content_length = sum(len(field) for field in fields) + len(file_header) + source_size + len(closing)

    def body_iter():
        for field in fields:
            yield field
        yield file_header
        for chunk in source_iter:
            if chunk:
                if progress:
                    progress(len(chunk))
                yield chunk
        yield closing

    return (
        StreamingMultipartBody(body_iter(), content_length),
        content_length,
        "multipart/form-data; boundary={}".format(boundary),
    )

