"""A registered graph directory that vanished is refused, never replaced."""

import pytest

from code_review_graph.errors import GraphStoreError
from code_review_graph.incremental import get_data_dir, get_db_path
from code_review_graph.registry import Registry


@pytest.mark.parametrize("create", [True, False])
def test_vanished_registered_dir_refuses_the_older_local_graph(tmp_path, monkeypatch, create):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").write_bytes(b"existing graph")
    missing = tmp_path / "swept" / "data"
    Registry().set_data_dir(str(repo), str(missing))
    with pytest.raises(GraphStoreError, match="prune --apply") as raised:
        get_data_dir(repo, create=create)
    assert str(missing) in str(raised.value)
    assert str(local) in str(raised.value)
    assert not missing.exists()


def test_vanished_registered_dir_routes_to_the_shared_database(tmp_path, monkeypatch):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").write_bytes(b"existing graph")
    Registry().set_data_dir(str(repo), str(tmp_path / "swept"))
    shared = tmp_path / "shared.db"
    monkeypatch.setenv("CRG_SHARED_DB", str(shared))
    assert get_db_path(repo, read_only=True) == shared.resolve()


@pytest.mark.parametrize("create", [True, False])
def test_fresh_relocation_without_local_graph_is_respected(tmp_path, monkeypatch, create):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    Registry().set_data_dir(str(repo), str(external))
    assert get_data_dir(repo, create=create) == external
    assert external.exists() == create


def test_existing_registered_directory_keeps_priority(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    local = repo / ".code-review-graph"
    local.mkdir(parents=True)
    (local / "graph.db").touch()
    external = tmp_path / "external"
    external.mkdir()
    Registry().set_data_dir(str(repo), str(external))
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "env"))
    assert get_data_dir(repo) == external


@pytest.mark.parametrize("exists", [True, False])
def test_repos_displays_registered_data_directory(tmp_path, monkeypatch, capsys, exists):
    import sys

    from code_review_graph.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    if exists:
        external.mkdir()
    Registry().set_data_dir(str(repo), str(external))
    monkeypatch.setattr(sys, "argv", ["code-review-graph", "repos"])
    main()
    output = capsys.readouterr().out
    assert f"data_dir: {external}" in output
    assert ("[MISSING]" in output) == (not exists)
