from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from another_trade.io import atomic_write_bytes, atomic_write_json


@dataclass(frozen=True, slots=True)
class CachedResponse:
    raw: bytes
    sha256: str
    metadata: dict[str, object]


class ImmutableResponseCache:
    """Content-addressed raw-response cache for immutable historical requests."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def key(
        *,
        base_url: str,
        method: str,
        path: str,
        params: Mapping[str, str | int],
    ) -> str:
        canonical = json.dumps(
            {
                "base_url": base_url,
                "method": method.upper(),
                "path": path,
                "params": sorted((str(k), str(v)) for k, v in params.items()),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _paths(self, key: str) -> tuple[Path, Path]:
        shard = self.root / key[:2] / key[2:4]
        return shard / f"{key}.body", shard / f"{key}.meta.json"

    def get(self, key: str) -> CachedResponse | None:
        body_path, meta_path = self._paths(key)
        if not body_path.exists() or not meta_path.exists():
            return None
        raw = body_path.read_bytes()
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(raw).hexdigest()
        if digest != metadata.get("sha256"):
            raise RuntimeError(f"cache corruption for key {key}")
        return CachedResponse(raw=raw, sha256=digest, metadata=metadata)

    def put(
        self,
        *,
        key: str,
        raw: bytes,
        request_metadata: Mapping[str, object],
    ) -> CachedResponse:
        body_path, meta_path = self._paths(key)
        digest = hashlib.sha256(raw).hexdigest()
        metadata: dict[str, object] = {
            **request_metadata,
            "sha256": digest,
            "bytes": len(raw),
        }
        atomic_write_bytes(body_path, raw)
        atomic_write_json(meta_path, metadata)
        return CachedResponse(raw=raw, sha256=digest, metadata=metadata)
