from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    version: str
    sha256: str
    relationships: tuple[dict[str, Any], ...]


def load_identity_config() -> IdentityConfig:
    resource = files("another_trade.config").joinpath("identity_relationships.json")
    raw = resource.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("identity relationship config must be a JSON object")

    version = payload.get("version")
    relationships = payload.get("relationships")
    if not isinstance(version, str) or not version:
        raise ValueError("identity relationship config requires non-empty version")
    if not isinstance(relationships, list):
        raise ValueError("identity relationship config requires relationships list")

    validated: list[dict[str, Any]] = []
    for item in relationships:
        if not isinstance(item, dict):
            raise ValueError("identity relationship entry must be an object")
        for key in (
            "left_symbol",
            "right_symbol",
            "relation",
            "confidence",
            "automatic_stitching",
            "source_urls",
            "evidence_notes",
        ):
            if key not in item:
                raise ValueError(f"identity relationship missing {key}")
        if item["automatic_stitching"] is not False:
            raise ValueError("identity relationships must not enable automatic stitching")
        if not isinstance(item["source_urls"], list) or not item["source_urls"]:
            raise ValueError("identity relationship requires source_urls")
        validated.append(dict(item))

    return IdentityConfig(
        version=version,
        sha256=hashlib.sha256(raw).hexdigest(),
        relationships=tuple(validated),
    )
