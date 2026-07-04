"""Shared exceptions and retry classification."""

from __future__ import annotations

import requests


class ThuCloudError(Exception):
    """Base exception for this project."""


class TransferInterrupted(ThuCloudError):
    """Transfer stopped by user request."""


class TransferVerificationError(ThuCloudError):
    """Transfer finished locally but failed a post-transfer verification."""


class SourceChangedError(ThuCloudError):
    """Local source changed while a transfer was in progress."""


class ApiError(ThuCloudError):
    """HTTP API error with a short response body."""

    def __init__(self, context: str, response: requests.Response, detail: str):
        self.context = context
        self.response = response
        self.status_code = response.status_code
        self.detail = detail
        super().__init__(
            "{} failed: {} {}: {}".format(
                context,
                response.status_code,
                response.reason,
                detail,
            )
        )


def raise_for_status_with_body(resp: requests.Response, context: str) -> None:
    if resp.ok:
        return
    detail = resp.text[:500].replace("\n", " ")
    raise ApiError(context, resp, detail)


def is_transient_error(exc: BaseException) -> bool:
    """Return True for errors where retrying the same transfer is reasonable."""

    if isinstance(exc, ApiError):
        detail = exc.detail.lower()
        if exc.status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
            return True
        if exc.status_code == 403 and "access token not found" in detail:
            return True
        return False

    if isinstance(
        exc,
        (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
            requests.exceptions.SSLError,
            requests.exceptions.Timeout,
        ),
    ):
        return True

    return False


def scrub_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return "{}...{}".format(value[:4], value[-4:])
