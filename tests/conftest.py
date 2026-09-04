"""Shared test fixtures that isolate CRG's user-level state."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_crg_home(tmp_path_factory, monkeypatch):
    """Keep CRG and Hermes test state outside the developer's real home."""
    home = tmp_path_factory.mktemp("crg-home")
    monkeypatch.setenv("CRG_HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path_factory.mktemp("hermes-home")))
    return home
