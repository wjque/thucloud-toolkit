"""Small JSON manifest for resumable transfer bookkeeping."""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any


class ManifestStore:
    def __init__(self, cache_dir: str):
        self.root = os.path.join(cache_dir, "manifests")
        os.makedirs(self.root, exist_ok=True)

    def key(self, *items: str) -> str:
        digest = hashlib.sha256()
        for item in items:
            digest.update(item.encode("utf-8", errors="replace"))
            digest.update(b"\0")
        return digest.hexdigest()[:24]

    def path(self, manifest_id: str) -> str:
        return os.path.join(self.root, "{}.json".format(manifest_id))

    def load(self, manifest_id: str) -> dict[str, Any]:
        path = self.path(manifest_id)
        if not os.path.exists(path):
            return {"id": manifest_id, "parts": {}}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def part_state(self, manifest_id: str, part_name: str) -> str | None:
        part = self.part_info(manifest_id, part_name)
        if not isinstance(part, dict):
            return None
        state = part.get("state")
        return str(state) if state is not None else None

    def part_info(self, manifest_id: str, part_name: str) -> dict[str, Any] | None:
        part = self.load(manifest_id).get("parts", {}).get(part_name)
        return part if isinstance(part, dict) else None

    def save(self, manifest_id: str, data: dict[str, Any]) -> None:
        path = self.path(manifest_id)
        tmp_path = "{}.tmp".format(path)
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, path)

    def update_part(
        self,
        manifest_id: str,
        part_name: str,
        *,
        state: str,
        expected_size: int,
        error: str | None = None,
        source_sha256: str | None = None,
    ) -> None:
        data = self.load(manifest_id)
        parts = data.setdefault("parts", {})
        item = parts.setdefault(part_name, {})
        item.update(
            {
                "state": state,
                "expected_size": expected_size,
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        )
        if error:
            item["error"] = error
        elif "error" in item:
            del item["error"]
        if source_sha256:
            item["source_sha256"] = source_sha256
        self.save(manifest_id, data)
