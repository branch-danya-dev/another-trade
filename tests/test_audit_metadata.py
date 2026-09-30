from __future__ import annotations

from another_trade.audit.coverage import LaunchTimeQuality, _launch_time_quality
from another_trade.audit.identity import load_identity_config


def test_large_launch_delay_is_marked_placeholder() -> None:
    day_ms = 86_400_000
    assert (
        _launch_time_quality(
            launch_ms=0,
            first_trade_ms=365 * day_ms,
        )
        is LaunchTimeQuality.PLACEHOLDER_SUSPECTED
    )


def test_normal_launch_delay_is_not_placeholder() -> None:
    day_ms = 86_400_000
    assert (
        _launch_time_quality(
            launch_ms=0,
            first_trade_ms=10 * day_ms,
        )
        is LaunchTimeQuality.OBSERVED_METADATA
    )


def test_identity_relationship_config_is_versioned_and_sourced() -> None:
    config = load_identity_config()
    assert config.version
    assert len(config.sha256) == 64
    assert config.relationships

    for relationship in config.relationships:
        assert relationship["automatic_stitching"] is False
        assert relationship["source_urls"]
        assert relationship["confidence"]
        assert relationship["evidence_notes"]
