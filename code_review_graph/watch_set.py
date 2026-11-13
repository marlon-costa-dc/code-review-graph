"""One process watching every repository a manifest names.

``code-review-graph watch --manifest PATH`` runs :func:`incremental.watch` for
each listed repository on its own thread inside a single process, instead of
one ``watch`` process per repository.  The manifest is re-read on every tick:
a repository added to it starts watching, a removed one stops, and the others
keep their observers.  Initial builds run one at a time, so a host with many
repositories never parses all of them at once.

A watcher failure stops every watcher and exits non-zero, exactly as the
single-repository ``watch`` does: the supervisor restarts the process instead
of letting one graph go quietly stale.

Manifest format (JSON, written atomically by its owner)::

    {"repos": [{"repo": "/abs/repo", "data_dir": "/abs/data"}]}
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .graph import GraphStore
from .incremental import _WATCH_STOP_TIMEOUT, _WATCH_TICK_SECONDS, watch

logger = logging.getLogger(__name__)

_DATABASE_NAME = "graph.db"


@dataclass(frozen=True)
class WatchTarget:
    """One repository and the directory holding its graph database."""

    repo: Path
    data_dir: Path


def load_manifest(path: Path) -> tuple[WatchTarget, ...]:
    """Parse the manifest into canonical, unique watch targets.

    Raises:
        ValueError: if the document is not the declared shape, a path is not
            absolute, a database is missing, or a repository or data
            directory appears twice.
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"repos"}:
        raise ValueError(f"watch manifest {path} must be an object with only 'repos'")
    entries = document["repos"]
    if not isinstance(entries, list):
        raise ValueError(f"watch manifest {path}: 'repos' must be a list")
    targets: list[WatchTarget] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"repo", "data_dir"}:
            raise ValueError(
                f"watch manifest {path}: every entry needs exactly 'repo' and 'data_dir'"
            )
        repo, data_dir = Path(entry["repo"]), Path(entry["data_dir"])
        if not repo.is_absolute() or not data_dir.is_absolute():
            raise ValueError(f"watch manifest {path}: paths must be absolute: {entry}")
        target = WatchTarget(repo=repo.resolve(strict=True), data_dir=data_dir.resolve())
        if not (target.data_dir / _DATABASE_NAME).is_file():
            raise ValueError(
                f"watch manifest {path}: no graph at {target.data_dir / _DATABASE_NAME}; "
                "build it before watching"
            )
        targets.append(target)
    for name, values in (
        ("repository", [target.repo for target in targets]),
        ("data directory", [target.data_dir for target in targets]),
    ):
        if len(set(values)) != len(values):
            raise ValueError(f"watch manifest {path}: duplicate {name}")
    return tuple(targets)


def _manifest_version(path: Path) -> tuple[int, int, int]:
    """Return the identity that changes whenever the manifest is replaced."""
    stat = path.stat()
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


@dataclass
class _Watcher:
    """One repository's watch thread and the event that stops it."""

    target: WatchTarget
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    failure: BaseException | None = None


def watch_set(
    manifest: Path,
    on_files_updated: Callable[[GraphStore], Any] | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Watch every repository the manifest names from this one process.

    Args:
        manifest: JSON manifest of ``{"repo", "data_dir"}`` entries.
        on_files_updated: Post-processing callback handed to each watcher.
        stop_event: Optional event that ends every watcher cleanly.

    Raises:
        RuntimeError: if any repository watcher fails; every other watcher is
            stopped first and the first failure is the one raised.
    """
    manifest = manifest.resolve(strict=True)
    stopping = stop_event if stop_event is not None else threading.Event()
    startup_lock = threading.Lock()
    watchers: dict[Path, _Watcher] = {}

    def run(watcher: _Watcher) -> None:
        store = GraphStore(watcher.target.data_dir / _DATABASE_NAME)
        try:
            watch(
                watcher.target.repo,
                store,
                on_files_updated=on_files_updated,
                stop_event=watcher.stop,
                startup_lock=startup_lock,
            )
        except BaseException as failure:  # recorded for the main loop to raise
            # A stop requested while queued behind another repository's
            # initial build unwinds as KeyboardInterrupt; that is a clean stop.
            if not (isinstance(failure, KeyboardInterrupt) and watcher.stop.is_set()):
                watcher.failure = failure
        finally:
            store.close()

    def start(target: WatchTarget) -> None:
        watcher = _Watcher(target=target)
        watcher.thread = threading.Thread(
            target=run,
            args=(watcher,),
            name=f"crg-watch:{target.repo.name}",
            daemon=True,
        )
        watchers[target.repo] = watcher
        watcher.thread.start()
        logger.info("Watching %s (graph %s)", target.repo, target.data_dir)

    def stop(repo: Path) -> None:
        watcher = watchers.pop(repo)
        watcher.stop.set()
        if watcher.thread is not None:
            watcher.thread.join(timeout=_WATCH_STOP_TIMEOUT * 2)
            if watcher.thread.is_alive():
                raise TimeoutError(f"watcher for {repo} did not stop")
        if watcher.failure is not None:
            raise RuntimeError(f"watch for {repo} failed") from watcher.failure
        logger.info("Stopped watching %s", repo)

    def reconcile(targets: tuple[WatchTarget, ...]) -> None:
        desired = {target.repo: target for target in targets}
        for repo in [repo for repo in watchers if desired.get(repo) != watchers[repo].target]:
            stop(repo)
        for repo, target in desired.items():
            if repo not in watchers:
                start(target)

    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    try:
        version = _manifest_version(manifest)
        reconcile(load_manifest(manifest))
        logger.info("Watching %d repositories from one process", len(watchers))
        while not stopping.wait(_WATCH_TICK_SECONDS):
            failed = [watcher for watcher in watchers.values() if watcher.failure is not None]
            if failed:
                first = failed[0]
                raise RuntimeError(f"watch for {first.target.repo} failed") from first.failure
            current = _manifest_version(manifest)
            if current != version:
                version = current
                reconcile(load_manifest(manifest))
                logger.info("Manifest reloaded: %d repositories", len(watchers))
    except KeyboardInterrupt:
        stopping.set()
    finally:
        active = sys.exc_info()[1]
        cleanup_failure: BaseException | None = None
        for repo in list(watchers):
            try:
                stop(repo)
            except BaseException as failure:
                if active is not None:
                    # The original failure stays the one raised; this one is
                    # logged with its traceback so it is not lost.
                    logger.exception("Stopping the watcher for %s also failed", repo)
                elif cleanup_failure is None:
                    cleanup_failure = failure
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)
        if cleanup_failure is not None:
            raise cleanup_failure
