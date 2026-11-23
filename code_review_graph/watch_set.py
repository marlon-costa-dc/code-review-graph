"""One process watching every repository a manifest names.

``code-review-graph watch --manifest PATH`` runs :func:`incremental.watch` for
each listed repository on its own thread inside a single process, instead of
one ``watch`` process per repository.  Manifest changes wake reconciliation:
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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .daemon import ConfigWatcher
from .graph import GraphStore
from .incremental import _WATCH_STOP_TIMEOUT, _WATCH_TICK_SECONDS, _wait_for_watch_write_lock, watch
from .locking import WriteLock
from .shared_sqlite import shared_database

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
    shared = shared_database()
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
        if shared is None and not (target.data_dir / _DATABASE_NAME).is_file():
            raise ValueError(
                f"watch manifest {path}: no graph at {target.data_dir / _DATABASE_NAME}; "
                "build it before watching"
            )
        targets.append(target)
    for name, values in (
        ("repository", [target.repo for target in targets]),
        ("data directory", [target.data_dir for target in targets] if shared is None else []),
    ):
        if len(set(values)) != len(values):
            raise ValueError(f"watch manifest {path}: duplicate {name}")
    return tuple(targets)


def _manifest_version(path: Path) -> tuple[int, int, int]:
    """Return the identity that changes whenever the manifest is replaced."""
    stat = path.stat()
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


def wait_ready(repo: Path, timeout: int) -> dict[str, str]:
    """Wait on the native health publication and verify the selected graph."""
    from .build_state import BUILD_COMPLETE, BUILD_STATE_KEY
    from .constants import normalize_file_path
    from .daemon import pid_alive, read_watch_health, watch_health_path
    from .errors import GraphStoreError
    from .incremental import _git_branch_info, _observe_source, collect_all_files, get_db_path
    from .shared_sqlite import UnbuiltCheckoutError

    root = repo.resolve(strict=True)
    health_path = watch_health_path(root)
    deadline = time.monotonic() + timeout
    timeout_message = f"Watch for {root} did not become ready within {timeout}s"

    def ready() -> dict[str, str] | None:
        health = read_watch_health(root)
        if (
            not health or health.get("repo") != str(root) or health.get("stalled")
            or health.get("phase") != "watching" or health.get("observer_alive") is not True
            or health.get("failed_paths") or not health.get("watched_paths")
            or type(health.get("pid")) is not int or health["pid"] <= 0
            or not isinstance(health.get("age"), (float, int)) or health["age"] < 0
            or not pid_alive(health["pid"])
        ):
            return None
        path = get_db_path(root, read_only=True)
        if not path.is_file():
            return None
        try:
            with GraphStore(path, checkout=root, read_only=True) as store:
                branch, head = _git_branch_info(root)
                if (
                    head and store.get_metadata(BUILD_STATE_KEY) == BUILD_COMPLETE
                    and store.get_metadata("git_head_sha") == head
                    and store.get_metadata("git_branch") == branch
                ):
                    # A worktree edit or revert need not change the Git anchor.
                    # Compare the native inventory to the bytes in the File nodes.
                    indexed = store.get_file_hashes()
                    files = collect_all_files(root)
                    if len(indexed) != len(files):
                        return None
                    for file in files:
                        source = root / file
                        if _observe_source(source).sha256 != indexed.get(
                            normalize_file_path(source),
                        ):
                            return None
                    if _git_branch_info(root) == (branch, head):
                        return {"repo": str(root), "head": head, "branch": branch}
        except (UnbuiltCheckoutError, FileNotFoundError):
            return None
        return None

    if (result := ready()) and time.monotonic() <= deadline:
        return result
    if time.monotonic() >= deadline:
        raise GraphStoreError(timeout_message)
    health_path.parent.mkdir(parents=True, exist_ok=True)
    notified = threading.Event()
    observer = ConfigWatcher(health_path, notified.set)
    observer.start()
    try:
        while True:
            notified.clear()
            if (result := ready()) and time.monotonic() <= deadline:
                return result
            if not observer.is_alive():
                raise GraphStoreError(f"Readiness notification backend stopped for {root}")
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not notified.wait(remaining):
                if not observer.is_alive():
                    raise GraphStoreError(f"Readiness notification backend stopped for {root}")
                raise GraphStoreError(timeout_message)
    finally:
        observer.stop()


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
    *,
    initialize_embeddings: bool = False,
) -> None:
    """Watch every repository the manifest names from this one process.

    Args:
        manifest: JSON manifest of ``{"repo", "data_dir"}`` entries.
        on_files_updated: Post-processing callback handed to each watcher.
        stop_event: Optional event that ends every watcher cleanly.
        initialize_embeddings: Initialize indices of already-current checkouts.

    Raises:
        RuntimeError: if any repository watcher fails; every other watcher is
            stopped first and the first failure is the one raised.
    """
    manifest = manifest.resolve(strict=True)
    stopping = stop_event if stop_event is not None else threading.Event()
    startup_lock = threading.Lock()
    watchers: dict[Path, _Watcher] = {}
    # Removed from the manifest but still unwinding. Reap on a later tick so
    # waiting for a parser batch cannot stall or tear down the other watches.
    retiring: list[_Watcher] = []
    desired: dict[Path, WatchTarget] = {}
    changed = threading.Event()
    observer = ConfigWatcher(manifest, changed.set)
    shared = shared_database()

    def run(watcher: _Watcher, store: GraphStore | None) -> None:
        try:
            if store is None:
                store = GraphStore(watcher.target.data_dir / _DATABASE_NAME)
            watch(
                watcher.target.repo,
                store,
                on_files_updated=on_files_updated,
                stop_event=watcher.stop,
                startup_lock=startup_lock,
                initialize_embeddings=initialize_embeddings,
            )
        except BaseException as failure:  # recorded for the main loop to raise
            if not (isinstance(failure, KeyboardInterrupt) and watcher.stop.is_set()):
                watcher.failure = failure
        finally:
            if store is not None:
                store.close()

    def start(target: WatchTarget, store: GraphStore | None = None) -> None:
        watcher = _Watcher(target=target)
        watcher.thread = threading.Thread(
            target=run,
            args=(watcher, store),
            name=f"crg-watch:{target.repo.name}",
            daemon=True,
        )
        watcher.thread.start()
        watchers[target.repo] = watcher
        logger.info("Watching %s (graph %s)", target.repo, target.data_dir)

    def start_shared(targets: list[WatchTarget], path: Path) -> None:
        # Opening a writable store can migrate both the file and its checkout
        # schema. Finish every open under the native writer lock before any
        # of these observers can queue a write or spawn parser workers.
        opened: dict[WatchTarget, GraphStore] = {}
        lock = WriteLock(path)
        if not _wait_for_watch_write_lock(lock, stopping):
            return
        try:
            try:
                for target in targets:
                    if stopping.is_set():
                        return
                    try:
                        opened[target] = GraphStore(path, checkout=target.repo)
                    except Exception as exc:
                        raise RuntimeError(
                            f"watch for {target.repo} failed to open {path}",
                        ) from exc
            finally:
                lock.release()
            for target in targets:
                if stopping.is_set():
                    return
                start(target, opened[target])
                del opened[target]
        finally:
            for store in opened.values():
                store.close()

    def raise_failure(watcher: _Watcher) -> None:
        if watcher.failure is not None:
            raise RuntimeError(f"watch for {watcher.target.repo} failed") from watcher.failure

    def reap() -> None:
        for watcher in [item for item in retiring if not item.thread or not item.thread.is_alive()]:
            retiring.remove(watcher)
            raise_failure(watcher)
            logger.info("Stopped watching %s", watcher.target.repo)

    def reconcile() -> None:
        for repo in [repo for repo in watchers if desired.get(repo) != watchers[repo].target]:
            watcher = watchers.pop(repo)
            watcher.stop.set()
            retiring.append(watcher)
        reap()
        unwinding = {watcher.target.repo for watcher in retiring}
        # A repository still unwinding keeps its graph until it is reaped.
        pending = [target for repo, target in desired.items()
                   if repo not in watchers and repo not in unwinding]
        if pending and shared is not None:
            start_shared(pending, shared)
        else:
            for target in pending:
                start(target)

    def stop_all(*, report_failures: bool) -> BaseException | None:
        """Stop and join every watcher; return the first problem found.

        Every watcher gets the stop signal before joins use a shared deadline.
        A watcher failure takes precedence over timeouts during shutdown.
        """
        everyone = [*watchers.values(), *retiring]
        for watcher in everyone:
            watcher.stop.set()
        deadline = time.monotonic() + _WATCH_STOP_TIMEOUT * 2
        timed_out = []
        for watcher in everyone:
            if watcher.thread is not None:
                watcher.thread.join(timeout=max(0.0, deadline - time.monotonic()))
                if watcher.thread.is_alive():
                    timed_out.append(str(watcher.target.repo))
        timeout = (
            TimeoutError(f"Watchers did not stop: {', '.join(timed_out)}") if timed_out else None
        )
        if report_failures:
            for watcher in everyone:
                if watcher.failure is not None:
                    first = RuntimeError(f"watch for {watcher.target.repo} failed")
                    first.__cause__ = watcher.failure
                    if timeout is not None:
                        logger.error("Stopping the watchers also failed: %s", timeout)
                    return first
        return timeout

    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    try:
        observer.start()
        version = _manifest_version(manifest)
        desired = {target.repo: target for target in load_manifest(manifest)}
        reconcile()
        logger.info("Watching %d repositories from one process", len(watchers))
        while not stopping.is_set():
            notified = changed.wait(_WATCH_TICK_SECONDS)
            if stopping.is_set():
                break
            if not observer.is_alive():
                raise RuntimeError(f"Manifest notification backend stopped: {manifest}")
            for watcher in watchers.values():
                raise_failure(watcher)
            if notified:
                changed.clear()
                current = _manifest_version(manifest)
                if current != version:
                    version = current
                    desired = {target.repo: target for target in load_manifest(manifest)}
                    logger.info("Manifest reloaded: %d repositories", len(desired))
            reconcile()
    except KeyboardInterrupt:
        stopping.set()
    finally:
        active = sys.exc_info()[1]
        try:
            problem = stop_all(report_failures=active is None)
            try:
                observer.stop()
            except Exception:
                if active is None and problem is None:
                    raise
                logger.exception("Stopping the manifest observer also failed")
            if problem is not None:
                if active is None:
                    raise problem
                # The original failure stays the one raised; this one is
                # logged so it is not lost.
                logger.error("Stopping the watchers also failed: %s", problem)
        finally:
            if previous_handler is not None:
                signal.signal(signal.SIGTERM, previous_handler)
