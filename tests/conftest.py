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


@pytest.fixture
def watch_repository():
    """Own a real watcher, independent graph reader and failure propagation."""
    import threading
    from collections.abc import Callable, Iterator
    from contextlib import contextmanager
    from pathlib import Path

    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import ObserverProtocol, watch

    @contextmanager
    def observe(
        repo: Path,
        on_files_updated: Callable[[GraphStore], object] | None = None,
        *,
        observer: ObserverProtocol | None = None,
    ) -> Iterator[tuple[GraphStore, threading.Thread]]:
        with GraphStore(repo / "graph.db") as store, GraphStore(repo / "graph.db") as reader:
            stop = threading.Event()
            failures: list[BaseException] = []

            def run_watch() -> None:
                try:
                    watch(
                        repo,
                        store,
                        on_files_updated=on_files_updated,
                        stop_event=stop,
                        observer=observer,
                    )
                except BaseException as exc:
                    failures.append(exc)

            thread = threading.Thread(target=run_watch, name="watch-under-test", daemon=True)
            thread.start()
            try:
                yield reader, thread
            finally:
                stop.set()
                thread.join(timeout=20)
                assert not thread.is_alive(), "the watch thread did not stop"
                if failures:
                    raise failures[0]

    return observe
