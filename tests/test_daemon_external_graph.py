"""The daemon must reuse the same external graph its child will open."""

import subprocess
import sys
from unittest.mock import patch

import pytest

from code_review_graph.daemon import DaemonConfig, WatchDaemon, WatchRepo
from code_review_graph.graph import GraphStore
from code_review_graph.locking import WriteLock
from code_review_graph.registry import Registry


@pytest.mark.parametrize("checkout_owner", [False, True])
def test_lock_excludes_other_processes_until_release(tmp_path, checkout_owner):
    db = tmp_path / "graph.db"
    path = tmp_path / "watch.lock" if checkout_owner else None
    owner = WriteLock(db, owner_path=path)
    probe = [
        sys.executable, "-c",
        "from pathlib import Path; from sys import argv; "
        "from code_review_graph.locking import WriteLock; "
        "lock = WriteLock(Path(argv[1]), owner_path=Path(argv[2]) if argv[2] else None); "
        "print(lock.acquire())",
        str(db), str(path) if path is not None else "",
    ]
    assert owner.acquire()
    try:
        assert subprocess.check_output(probe, text=True, timeout=10).strip() == "False"
    finally:
        owner.release()
    assert subprocess.check_output(probe, text=True, timeout=10).strip() == "True"


@pytest.mark.parametrize("operation", ["start", "reconcile"])
@pytest.mark.parametrize("placement", ["registry", "environment"])
def test_daemon_reuses_external_graph(tmp_path, monkeypatch, operation, placement):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data = tmp_path / "external-graph"
    data.mkdir()
    with GraphStore(data / "graph.db") as store:
        store.set_metadata("reuse-witness", "keep-existing-index")
        store.commit()
    if placement == "registry":
        Registry().register(str(repo), data_dir=str(data))
    else:
        monkeypatch.setenv("CRG_DATA_DIR", str(data))

    config = DaemonConfig(repos=[WatchRepo(path=str(repo), alias="repo")])
    daemon = WatchDaemon(config=config, config_path=tmp_path / "watch.toml")
    try:
        with (
            patch.object(daemon, "_initial_build") as build,
            patch.object(daemon, "_start_watcher") as watch,
            patch.object(daemon, "start_config_watcher"),
            patch.object(daemon, "start_health_checker"),
        ):
            getattr(daemon, operation)()
            build.assert_not_called()
            watch.assert_called_once_with(config.repos[0])
        assert not (repo / ".code-review-graph").exists()
        with GraphStore(data / "graph.db") as store:
            assert store.get_metadata("reuse-witness") == "keep-existing-index"
    finally:
        daemon.stop()


@pytest.mark.parametrize("operation", ["start", "reconcile"])
def test_daemon_does_not_build_while_another_watcher_initializes(tmp_path, operation):
    (tmp_path / ".git").mkdir()
    repo = WatchRepo(path=str(tmp_path), alias="repo")
    daemon = WatchDaemon(DaemonConfig(repos=[repo]), config_path=tmp_path / "watch.toml")
    try:
        with (
            patch.object(daemon, "_existing_watcher", return_value=123),
            patch.object(daemon, "_initial_build") as build,
            patch.object(daemon, "start_config_watcher"),
            patch.object(daemon, "start_health_checker"),
            patch("code_review_graph.daemon.subprocess.Popen") as spawn,
        ):
            getattr(daemon, operation)()
            daemon._check_health()
            build.assert_not_called()
            spawn.assert_not_called()
            assert daemon.restart_count(repo.alias) == 0
    finally:
        daemon.stop()
