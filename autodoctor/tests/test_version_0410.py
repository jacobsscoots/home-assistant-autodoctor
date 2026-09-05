from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from autodoctor import AUTODOCTOR_VERSION


def test_app_and_addon_metadata_versions_match() -> None:
    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    config = yaml.safe_load(config_path.read_text())
    assert AUTODOCTOR_VERSION == "0.4.10"
    assert config["version"] == AUTODOCTOR_VERSION
