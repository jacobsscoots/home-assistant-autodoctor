from __future__ import annotations

import sys
from pathlib import Path

import yaml


def test_v0410_keeps_automatic_repair_disabled_by_default() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    assert config["options"]["auto_apply_low_risk"] is False
    assert config["options"]["repair_executor_enabled"] is False
