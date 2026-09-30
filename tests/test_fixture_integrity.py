from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "bybit"


def test_real_fixture_manifest_hashes_match_raw_bytes() -> None:
    manifest_path = FIXTURES / "manifest.json"
    if not manifest_path.exists():
        pytest.skip("real Bybit fixtures must be captured from an allowed local network")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.get("files", [])
    assert files
    for item in files:
        path = FIXTURES / item["file"]
        assert path.exists()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == item["sha256"]
