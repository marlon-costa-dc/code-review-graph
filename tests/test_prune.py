"""Tests for the prune command surface (code_review_graph.prune)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from code_review_graph.daemon import load_config
from code_review_graph.prune import prune
from code_review_graph.registry import Registry


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crg-home"
    home.mkdir()
    monkeypatch.setenv("CRG_HOME", str(home))
    return home


@pytest.fixture()
def live_and_dead(tmp_path: Path) -> tuple[Path, Path]:
    live = tmp_path / "live-repo"
    live.mkdir()
    _git(live, "init", "-q")
    dead = tmp_path / "dead-repo"
    dead.mkdir()
    (dead / ".git").mkdir()
    return live, dead


def test_prune_reports_dead_entries_without_applying(
    live_and_dead: tuple[Path, Path], home: Path
) -> None:
    live, dead = live_and_dead
    registry = Registry(home / "registry.json")
    registry.register(str(live))
    registry.register(str(dead))
    shutil.rmtree(dead)
    (home / "watch.toml").write_text(
        f'[daemon]\nsession_name = "crg-watch"\nlog_dir = "{home}/logs"\n'
        f'poll_interval = 2\n\n[[repos]]\npath = "{live}"\nalias = "live"\n\n'
        f'[[repos]]\npath = "{dead}"\nalias = "dead"\n',
        encoding="utf-8",
    )

    report = prune(registry=registry, config_path=home / "watch.toml", apply=False)

    assert report.applied is False
    assert report.removed_registry == [str(dead)]
    assert report.removed_watch == [str(dead)]
    assert registry.find_by_path(str(dead)) is not None
    raw = (home / "watch.toml").read_text(encoding="utf-8")
    assert str(dead) in raw


def test_prune_apply_rewrites_registry_and_watch(
    live_and_dead: tuple[Path, Path], home: Path
) -> None:
    live, dead = live_and_dead
    registry = Registry(home / "registry.json")
    registry.register(str(live))
    registry.register(str(dead))
    shutil.rmtree(dead)
    (home / "watch.toml").write_text(
        f'[daemon]\nsession_name = "crg-watch"\nlog_dir = "{home}/logs"\n'
        f'poll_interval = 2\n\n[[repos]]\npath = "{live}"\nalias = "live"\n\n'
        f'[[repos]]\npath = "{dead}"\nalias = "dead"\n',
        encoding="utf-8",
    )

    report = prune(registry=registry, config_path=home / "watch.toml", apply=True)

    assert report.applied is True
    assert report.removed_registry == [str(dead)]
    assert report.removed_watch == [str(dead)]
    assert registry.find_by_path(str(dead)) is None
    assert registry.find_by_path(str(live)) is not None
    config = load_config(home / "watch.toml")
    assert [repo.path for repo in config.repos] == [str(live)]


def test_prune_detects_orphan_external_data_dirs(
    live_and_dead: tuple[Path, Path], home: Path, tmp_path: Path
) -> None:
    live, dead = live_and_dead
    orphan_dir = tmp_path / "ext-data"
    orphan_dir.mkdir()
    registry = Registry(home / "registry.json")
    registry.register(str(live))
    registry.register(str(dead), data_dir=str(orphan_dir))
    shutil.rmtree(dead)

    report = prune(registry=registry, apply=False)

    assert (
        orphan_dir in [Path(p) for p in report.orphan_data_dirs]
        or str(orphan_dir) in report.orphan_data_dirs
    )
    assert orphan_dir.is_dir()


def test_prune_apply_with_data_dirs_removes_orphans(
    live_and_dead: tuple[Path, Path], home: Path, tmp_path: Path
) -> None:
    live, dead = live_and_dead
    orphan_dir = tmp_path / "ext-data"
    orphan_dir.mkdir()
    registry = Registry(home / "registry.json")
    registry.register(str(live))
    registry.register(str(dead), data_dir=str(orphan_dir))
    shutil.rmtree(dead)

    report = prune(registry=registry, apply=True, data_dirs=True)

    assert not orphan_dir.exists()
    assert report.removed_registry == [str(dead)]
