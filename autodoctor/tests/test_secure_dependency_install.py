from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("source", ["ci", "container"])
def test_dependency_installs_require_hash_checked_wheels(source: str) -> None:
    if source == "ci":
        workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        install = next(
            step["run"]
            for step in workflow["jobs"]["test"]["steps"]
            if step.get("name") == "Install test dependencies"
        )
        expected_lock = "autodoctor/requirements-test.txt"
    else:
        dockerfile = (ROOT / "autodoctor/Dockerfile").read_text().replace("\\\n", " ")
        instruction = next(
            line for line in dockerfile.splitlines() if "python3 -m pip install" in line
        )
        install = instruction.split("&&")[-1]
        expected_lock = "/tmp/requirements.txt"
    arguments = shlex.split(install)
    assert arguments[1:4] == ["-m", "pip", "install"]
    assert "--require-hashes" in arguments
    assert "--only-binary=:all:" in arguments
    assert arguments[-2:] == ["-r", expected_lock]
