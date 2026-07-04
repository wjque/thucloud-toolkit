"""Tsinghua Cloud API client.

The service is compatible with the Seafile API for the operations used here.
"""

from __future__ import annotations

import getpass
import posixpath
from typing import Any

import requests

from .config import DEFAULT_CLOUD_URL
from .errors import raise_for_status_with_body


def normalize_cloud_url(cloud_url: str) -> str:
    return cloud_url.rstrip("/")


def normalize_remote_dir(remote_dir: str) -> str:
    if not remote_dir:
        return "/"
    parts = [part for part in remote_dir.replace("\\", "/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("Remote directory cannot contain . or ..: {}".format(remote_dir))
    return "/" + "/".join(parts) if parts else "/"


def normalize_remote_path(path: str) -> str:
    if not path:
        raise ValueError("Remote path cannot be empty")
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("Remote path cannot contain . or ..: {}".format(path))
    return "/" + "/".join(parts)


def join_remote_path(remote_dir: str, name: str) -> str:
    return posixpath.join(normalize_remote_dir(remote_dir), name).replace("//", "/")


class CloudClient:
    def __init__(
        self,
        cloud_url: str = DEFAULT_CLOUD_URL,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        session: requests.Session | None = None,
    ):
        self.cloud_url = normalize_cloud_url(cloud_url)
        self.session = session or requests.Session()
        self.token = token or self._login(username, password)

    def api_url(self, path: str) -> str:
        return "{}{}".format(self.cloud_url, path)

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Token {}".format(self.token)}

    def _login(self, username: str | None, password: str | None) -> str:
        username = username or input("Tsinghua Cloud username: ")
        password = password or getpass.getpass("Tsinghua Cloud password: ")
        resp = self.session.post(
            self.api_url("/api2/auth-token/"),
            data={"username": username, "password": password},
        )
        raise_for_status_with_body(resp, "Login")
        return resp.json()["token"]

    def get(self, path: str, *, params: dict[str, Any] | None = None, context: str) -> requests.Response:
        resp = self.session.get(self.api_url(path), params=params, headers=self.auth_headers())
        raise_for_status_with_body(resp, context)
        return resp

    def post(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        context: str,
    ) -> requests.Response:
        resp = self.session.post(
            self.api_url(path),
            params=params,
            data=data,
            headers=self.auth_headers(),
        )
        raise_for_status_with_body(resp, context)
        return resp

    def list_repos(self) -> list[dict[str, Any]]:
        return self.get("/api2/repos/", context="List repos").json()

    def ensure_repo_access(self, repo_id: str) -> None:
        resp = self.session.get(
            self.api_url("/api2/repos/{}/dir/".format(repo_id)),
            params={"p": "/"},
            headers=self.auth_headers(),
        )
        if resp.status_code == 200:
            return
        if resp.status_code == 404:
            raise ValueError(
                "Cannot access repo root. Check --repo-id; it must be the library id, "
                "not a share key, folder name, or library name."
            )
        raise_for_status_with_body(resp, "Check repo access")

    def dir_exists(self, repo_id: str, remote_dir: str) -> bool:
        resp = self.session.get(
            self.api_url("/api2/repos/{}/dir/".format(repo_id)),
            params={"p": normalize_remote_dir(remote_dir)},
            headers=self.auth_headers(),
        )
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        raise_for_status_with_body(resp, "Check remote directory")
        return False

    def ensure_dir(self, repo_id: str, remote_dir: str) -> None:
        remote_dir = normalize_remote_dir(remote_dir)
        if remote_dir == "/":
            return

        current = ""
        for part in remote_dir.strip("/").split("/"):
            current = posixpath.join(current, part)
            current_path = "/" + current
            if self.dir_exists(repo_id, current_path):
                continue
            resp = self.session.post(
                self.api_url("/api2/repos/{}/dir/".format(repo_id)),
                params={"p": current_path},
                data={"operation": "mkdir"},
                headers=self.auth_headers(),
            )
            if resp.ok or self.dir_exists(repo_id, current_path):
                continue
            raise_for_status_with_body(resp, "Create remote directory {}".format(current_path))

    def list_dir(self, repo_id: str, remote_dir: str) -> list[dict[str, Any]]:
        resp = self.get(
            "/api2/repos/{}/dir/".format(repo_id),
            params={"p": normalize_remote_dir(remote_dir)},
            context="List remote directory {}".format(remote_dir),
        )
        entries = resp.json()
        if isinstance(entries, dict):
            entries = entries.get("dirent_list") or entries.get("children") or entries.get("entries") or []
        if not isinstance(entries, list):
            raise ValueError("Unexpected remote directory response: {}".format(entries))
        return entries

    def remote_file_sizes(self, repo_id: str, remote_dir: str) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for entry in self.list_dir(repo_id, remote_dir):
            if not isinstance(entry, dict):
                continue
            entry_type = entry.get("type")
            if entry_type not in {None, "file"} and not entry.get("is_file", False):
                continue
            name = entry.get("name")
            size = entry.get("size", entry.get("file_size"))
            if not name or size is None:
                continue
            try:
                sizes[str(name)] = int(size)
            except (TypeError, ValueError):
                continue
        return sizes

    def remote_file_size(self, repo_id: str, remote_path: str) -> int | None:
        remote_path = normalize_remote_path(remote_path)
        remote_dir = posixpath.dirname(remote_path) or "/"
        name = posixpath.basename(remote_path)
        return self.remote_file_sizes(repo_id, remote_dir).get(name)

    def get_upload_link(self, repo_id: str, remote_dir: str = "/") -> str:
        resp = self.get(
            "/api2/repos/{}/upload-link/".format(repo_id),
            params={"p": normalize_remote_dir(remote_dir)},
            context="Get upload link",
        )
        upload_link = resp.json()
        if not isinstance(upload_link, str):
            raise ValueError("Unexpected upload link response: {}".format(upload_link))
        return upload_link

    def get_download_link(self, repo_id: str, remote_path: str) -> str:
        resp = self.get(
            "/api2/repos/{}/file/".format(repo_id),
            params={"p": normalize_remote_path(remote_path)},
            context="Get download link {}".format(remote_path),
        )
        download_link = resp.json()
        if not isinstance(download_link, str):
            raise ValueError("Unexpected download link response: {}".format(download_link))
        return download_link

