"""The daemon must reuse the same external graph its child will open."""

from unittest.mock import patch

import pytest

from code_review_graph.daemon import DaemonConfig, WatchDaemon, WatchRepo
from code_review_graph.graph import GraphStore
from code_review_graph.registry import Registry


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
