"""Hermetic process-wide test configuration."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_crg_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep every default registry access inside the current test sandbox."""
    monkeypatch.setenv("CRG_REGISTRY_PATH", str(tmp_path / "registry.json"))
