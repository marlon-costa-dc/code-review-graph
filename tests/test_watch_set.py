"""One process watches every repository a manifest names (``watch --manifest``).

Real repositories, real graphs, the real watchdog observer: the tests prove
that both repositories are watched by this one process, that an edit reaches
the right graph, that replacing the manifest stops and starts watchers live,
and that a manifest naming an unbuilt graph is refused before anything runs.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from code_review_graph.daemon import read_watch_health, watch_health_path
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.watch_set import load_manifest, watch_set

_DEADLINE_SECONDS = 60.0


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "crg-home"))
    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")


def _repo(root: Path, name: str) -> tuple[Path, Path]:
    repo = root / name
    repo.mkdir()
    (repo / "app.py").write_text("def first():\n    return 1\n", encoding="utf-8")
    data_dir = root / f"{name}-graph"
    data_dir.mkdir()
    with GraphStore(data_dir / "graph.db") as store:
        full_build(repo, store)
    return repo, data_dir


def _write_manifest(path: Path, entries: list[tuple[Path, Path]]) -> None:
    staged = path.with_suffix(".tmp")
    staged.write_text(
        json.dumps({"repos": [{"repo": str(r), "data_dir": str(d)} for r, d in entries]}),
        encoding="utf-8",
    )
    os.replace(staged, path)


def _until(predicate, what: str) -> None:
    deadline = time.monotonic() + _DEADLINE_SECONDS
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.2)


def _watching(repo: Path) -> bool:
    health = read_watch_health(repo)
    return bool(health) and health.get("phase") == "watching"


def _has_function(data_dir: Path, name: str) -> bool:
    with GraphStore(data_dir / "graph.db") as store:
        return any(node.name == name for node in store.get_all_nodes())


class TestWatchSet:
    def test_one_process_watches_every_repo_and_follows_the_manifest(self, tmp_path):
        first, first_data = _repo(tmp_path, "first")
        second, second_data = _repo(tmp_path, "second")
        manifest = tmp_path / "watch.json"
        _write_manifest(manifest, [(first, first_data), (second, second_data)])
        stop = threading.Event()
        failures: list[BaseException] = []

        def run() -> None:
            try:
                watch_set(manifest, stop_event=stop)
            except BaseException as failure:  # surfaced to the test thread
                failures.append(failure)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            _until(lambda: _watching(first) and _watching(second), "both watchers")
            pids = {read_watch_health(repo)["pid"] for repo in (first, second)}
            assert pids == {os.getpid()}

            (first / "added.py").write_text("def added_later():\n    return 2\n", encoding="utf-8")
            _until(lambda: _has_function(first_data, "added_later"), "the edit in first")
            assert not _has_function(second_data, "added_later")

            _write_manifest(manifest, [(first, first_data)])
            _until(lambda: not watch_health_path(second).exists(), "second to stop")
            assert _watching(first)
        finally:
            stop.set()
            thread.join(timeout=_DEADLINE_SECONDS)
        assert not thread.is_alive()
        assert failures == []
        assert not watch_health_path(first).exists()

    def test_unbuilt_graph_is_refused_before_watching(self, tmp_path):
        repo = tmp_path / "unbuilt"
        repo.mkdir()
        empty = tmp_path / "unbuilt-graph"
        empty.mkdir()
        manifest = tmp_path / "watch.json"
        _write_manifest(manifest, [(repo, empty)])

        with pytest.raises(ValueError, match="build it before watching"):
            load_manifest(manifest)

    def test_duplicate_repository_is_refused(self, tmp_path):
        repo, data_dir = _repo(tmp_path, "dup")
        other = tmp_path / "other-graph"
        other.mkdir()
        (other / "graph.db").write_bytes((data_dir / "graph.db").read_bytes())
        manifest = tmp_path / "watch.json"
        _write_manifest(manifest, [(repo, data_dir), (repo, other)])

        with pytest.raises(ValueError, match="duplicate repository"):
            load_manifest(manifest)
