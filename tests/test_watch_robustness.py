"""Watch-mode robustness: ignore-aware scheduling and observer liveness.

Covers issue #811, where one recursive watch on the repository root made the
OS register a watch inside every ignored tree.  A build tool churning through
``target/`` then killed a watchdog thread, the process stayed up, and the graph
silently stopped updating while ``crg-daemon status`` still said "alive".

Supervisor unit cases isolate scheduling and liveness. The real-observer
cohort uses actual files, SQLite connections and OS events to prove indexing,
startup writes, directory recreation and deletion through the public watcher.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_review_graph.daemon import (
    DaemonConfig,
    WatchRepo,
    read_watch_health,
    watch_health_path,
    watcher_status,
)
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    _WATCH_SPLIT_MIN_DIRS,
    _load_ignore_patterns,
    _plan_watch_paths,
    _should_ignore,
    _WatchSupervisor,
    clear_nested_ignore_cache,
    incremental_update,
    watch,
)


@pytest.fixture(autouse=True)
def _fresh_nested_ignore_cache():
    """Nested build-output patterns are cached per repo; tests build repos."""
    clear_nested_ignore_cache()
    yield
    clear_nested_ignore_cache()


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeWatch:
    """Stand-in for watchdog's ObservedWatch."""

    def __init__(self, path: str, recursive: bool) -> None:
        self.path = path
        self.is_recursive = recursive


class FakeEmitter:
    """Stand-in for a watchdog emitter that owns a backend reader thread.

    inotify keeps its buffer thread exactly like this, and that buffer thread
    is the one that dies in #811 — the emitter itself stays alive.
    """

    def __init__(self, reader: threading.Thread | None = None, root: str | None = None) -> None:
        self._inotify = reader
        self.watch = FakeWatch(root, True) if root is not None else None


class FakeObserver:
    """Records what would have been handed to the OS."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[str, bool]] = []
        self.unscheduled: list[str] = []
        self.emitters: list[object] = []
        self.handler = None
        self.started = False
        self.stopped = False

    def schedule(self, handler, path, *, recursive=False, event_filter=None):
        self.handler = handler
        self.scheduled.append((path, recursive))
        return FakeWatch(path, recursive)

    def unschedule(self, watch_handle) -> None:
        self.unscheduled.append(watch_handle.path)
        # watchdog drops the emitter (and its threads) with the watch; the
        # liveness check depends on that, so the double must do it too.
        self.emitters = [
            emitter
            for emitter in self.emitters
            if getattr(getattr(emitter, "watch", None), "path", None) != watch_handle.path
        ]

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout=None) -> None:
        return None


def _live_thread() -> tuple[threading.Thread, threading.Event]:
    """A thread that stays alive until its event is set."""
    gate = threading.Event()
    thread = threading.Thread(target=gate.wait, name="fake-inotify-buffer", daemon=True)
    thread.start()
    return thread, gate


def _tick_driver(*actions):
    """Replacement for ``time.sleep`` that runs one scripted action per tick.

    After the script is exhausted the watch loop is stopped with a
    KeyboardInterrupt, which is how a real ``Ctrl+C`` leaves it.
    """
    state = {"tick": 0}

    def _sleep(_seconds):
        index = state["tick"]
        state["tick"] += 1
        if index >= len(actions):
            raise KeyboardInterrupt
        actions[index]()

    return _sleep


def _maven_repo(root: Path) -> None:
    """A monorepo shaped like the one in #811: nested modules with target/."""
    (root / ".git" / "objects").mkdir(parents=True)
    for index in range(8):
        (root / ".git" / "objects" / f"{index:02d}").mkdir()
    (root / "pom.xml").write_text("<project/>", encoding="utf-8")
    module = root / "intranet-backend"
    (module / "src" / "main" / "java").mkdir(parents=True)
    (module / "pom.xml").write_text("<project/>", encoding="utf-8")
    for index in range(6):
        (module / "target" / f"surefire-{index}").mkdir(parents=True)
    (module / "target" / "classes").mkdir(parents=True)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


class TestIgnoreAwareScheduling:
    def test_ignored_top_level_directories_are_never_scheduled(self, tmp_path):
        """node_modules and .git must not reach the OS watch list at all."""
        (tmp_path / "src").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True)
        (tmp_path / ".git" / "objects").mkdir(parents=True)

        plan = _plan_watch_paths(tmp_path, _load_ignore_patterns(tmp_path))
        paths = [str(path) for path, _ in plan]

        assert (tmp_path, False) in plan, "the repo root needs a non-recursive watch"
        assert (tmp_path / "src", True) in plan
        assert not any("node_modules" in path for path in paths)
        assert not any(".git" in path for path in paths)

    def test_root_watch_is_non_recursive_so_ignored_trees_stay_out(self, tmp_path):
        """A recursive root watch would re-register everything underneath it."""
        (tmp_path / "src").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True)

        plan = _plan_watch_paths(tmp_path, _load_ignore_patterns(tmp_path))

        assert (tmp_path, True) not in plan

    def test_clean_repository_keeps_a_single_recursive_watch(self, tmp_path):
        """Nothing to exclude means nothing to split: one watch, as before."""
        (tmp_path / "src" / "inner").mkdir(parents=True)

        plan = _plan_watch_paths(tmp_path, _load_ignore_patterns(tmp_path))

        assert plan == [(tmp_path, True)]

    def test_small_ignored_directory_does_not_buy_its_own_watch(self, tmp_path):
        """A lone __pycache__ costs one OS watch; a split costs a thread."""
        (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)

        plan = _plan_watch_paths(tmp_path, _load_ignore_patterns(tmp_path))

        assert plan == [(tmp_path, True)]

    def test_plan_falls_back_to_one_recursive_watch_when_over_budget(self, tmp_path):
        """The watch count is bounded; an over-budget repo keeps the old shape."""
        for index in range(10):
            (tmp_path / f"pkg{index}").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"dep{index}").mkdir(parents=True)

        plan = _plan_watch_paths(tmp_path, _load_ignore_patterns(tmp_path), max_schedules=3)

        assert plan == [(tmp_path, True)]

    def test_nested_module_output_is_ignored_and_never_watched(self, tmp_path):
        """`moduleA/target/` is build output when `moduleA/pom.xml` says so."""
        _maven_repo(tmp_path)
        patterns = _load_ignore_patterns(tmp_path)

        assert "/intranet-backend/target/**" in patterns
        assert _should_ignore("intranet-backend/target/surefire-0/a.xml", patterns)
        assert not _should_ignore("intranet-backend/src/main/java/A.java", patterns)

        plan = _plan_watch_paths(tmp_path, patterns)
        paths = [str(path) for path, _ in plan]

        assert not any("target" in path for path in paths)
        assert (tmp_path / "intranet-backend" / "src", True) in plan

    def test_nested_output_name_without_a_manifest_keeps_its_files(self, tmp_path):
        """Root anchoring stays intact for anyone whose nested target/ is source."""
        module = tmp_path / "moduleB"
        (module / "target").mkdir(parents=True)
        (module / "target" / "handler.py").write_text("x = 1\n", encoding="utf-8")

        patterns = _load_ignore_patterns(tmp_path)

        assert "/moduleB/target/**" not in patterns
        assert not _should_ignore("moduleB/target/handler.py", patterns)

    def test_root_level_output_dirs_still_match_the_anchored_pattern(self, tmp_path):
        """The nested scan adds patterns; it never removes existing ones."""
        patterns = _load_ignore_patterns(tmp_path)

        assert _should_ignore("target/classes/A.class", patterns)
        assert _should_ignore("build/output.js", patterns)
        assert not _should_ignore("src/build/output.js", patterns)

    def test_nested_scan_can_be_disabled(self, tmp_path, monkeypatch):
        _maven_repo(tmp_path)
        monkeypatch.setenv("CRG_NESTED_OUTPUT_SCAN", "0")
        clear_nested_ignore_cache()

        patterns = _load_ignore_patterns(tmp_path)

        assert "/intranet-backend/target/**" not in patterns

    def test_a_single_path_can_opt_out(self, tmp_path):
        """Dropping files from the graph needs an escape smaller than a kill switch."""
        _maven_repo(tmp_path)
        (tmp_path / ".code-review-graphignore").write_text(
            "# keep our hand-written module\n!intranet-backend/target\n", encoding="utf-8"
        )
        clear_nested_ignore_cache()

        patterns = _load_ignore_patterns(tmp_path)

        assert "/intranet-backend/target/**" not in patterns
        assert not _should_ignore("intranet-backend/target/Main.java", patterns)

    def test_excluded_directories_are_logged(self, tmp_path, caplog):
        """An exclusion drops files from the graph, so it has to be discoverable."""
        _maven_repo(tmp_path)
        clear_nested_ignore_cache()

        with caplog.at_level(logging.INFO, logger="code_review_graph.incremental"):
            _load_ignore_patterns(tmp_path)

        assert "intranet-backend/target" in caplog.text
        assert ".code-review-graphignore" in caplog.text


class TestNewDirectoryAdoption:
    """Adoption is driven by a per-tick listing, never by directory events.

    macOS drops every directory event for a child of a non-recursive watch, so
    an event-driven design is permanently blind to a new top-level directory.
    """

    def _supervisor(self, tmp_path, **kwargs) -> tuple[_WatchSupervisor, FakeObserver]:
        (tmp_path / "src").mkdir(exist_ok=True)
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True, exist_ok=True)
        observer = FakeObserver()
        supervisor = _WatchSupervisor(
            observer, tmp_path, _load_ignore_patterns(tmp_path), health_path=None, **kwargs
        )
        supervisor.schedule_initial(MagicMock())
        return supervisor, observer

    def test_a_quiet_tick_touches_nothing(self, tmp_path):
        supervisor, observer = self._supervisor(tmp_path)
        before = list(observer.scheduled)

        assert supervisor.sync_watches() == ([], [])
        assert observer.scheduled == before
        assert observer.unscheduled == []

    def test_relative_repo_root_still_adopts_and_releases(self, tmp_path, monkeypatch):
        """`--repo .` stays relative in the CLI; the supervisor must resolve it."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "src").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True)
        observer = FakeObserver()
        supervisor = _WatchSupervisor(
            observer, Path("."), _load_ignore_patterns(Path(".")), health_path=None
        )
        supervisor.schedule_initial(MagicMock())

        (tmp_path / "services").mkdir()
        adopted, _ = supervisor.sync_watches()

        assert adopted == [str(tmp_path / "services")]

        shutil.rmtree(tmp_path / "services")
        _, vanished = supervisor.sync_watches()

        assert vanished == [str(tmp_path / "services")]

    def test_adopted_subtree_is_planned_like_the_repository(self, tmp_path):
        """A module arriving from a branch switch must not re-register its junk.

        Watching an adopted directory recursively would hand `node_modules`
        and `target` straight back to the OS — the exposure #811 is about, for
        everything created after startup.
        """
        supervisor, observer = self._supervisor(tmp_path)

        module = tmp_path / "moduleA"
        (module / "src").mkdir(parents=True)
        for index in range(6):
            (module / "node_modules" / f"pkg{index}").mkdir(parents=True)
        adopted, _ = supervisor.sync_watches()

        assert adopted == [str(module)]
        assert (str(module), False) in observer.scheduled, "module needs a shallow watch"
        assert (str(module / "src"), True) in observer.scheduled
        assert not any("node_modules" in path for path, _ in observer.scheduled), (
            "the adopted module's node_modules was handed to the OS"
        )

    def test_a_tight_budget_narrows_the_plan_before_promoting(self, tmp_path):
        """A fix for #811 must not be able to re-create #811.

        Promoting the parent — usually the repository root — hands every
        ignored tree in the repository back to the OS. A coarser plan for the
        new directory alone costs one slot and leaves the rest filtered.
        """
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True)
        (tmp_path / "keep").mkdir()
        observer = FakeObserver()
        supervisor = _WatchSupervisor(
            observer,
            tmp_path,
            _load_ignore_patterns(tmp_path),
            health_path=None,
            max_schedules=3,
        )
        supervisor.schedule_initial(MagicMock())
        assert len(supervisor.watched_paths) == 2, "precondition: one slot left"

        module = tmp_path / "moduleA"
        (module / "src" / "lib").mkdir(parents=True)
        for index in range(6):
            (module / "src" / "node_modules" / f"pkg{index}").mkdir(parents=True)
        adopted, _ = supervisor.sync_watches()

        assert adopted == [str(module)]
        assert supervisor.degraded is False, "the repository root was promoted"
        assert (str(tmp_path), True) not in observer.scheduled
        assert str(tmp_path) in supervisor._shallow, "the root lost its filtering"
        assert (str(module), True) in observer.scheduled, "the new module is unwatched"

    def test_promotion_releases_grandchildren_too(self, tmp_path):
        """A stale grandchild watch duplicates every event and burns a slot."""
        (tmp_path / "src").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(parents=True)
        module = tmp_path / "moduleA"
        (module / "src").mkdir(parents=True)
        for index in range(6):
            (module / "node_modules" / f"pkg{index}").mkdir(parents=True)
        observer = FakeObserver()
        supervisor = _WatchSupervisor(
            observer,
            tmp_path,
            _load_ignore_patterns(tmp_path),
            health_path=None,
            max_schedules=4,
        )
        supervisor.schedule_initial(MagicMock())
        assert str(module / "src") in supervisor.watched_paths, "precondition: grandchild watch"

        (tmp_path / "services").mkdir()
        supervisor.sync_watches()

        assert supervisor.degraded is True
        assert supervisor.watched_paths == [str(tmp_path)], "grandchildren outlived the promotion"

    def test_budget_exhaustion_degrades_to_a_recursive_watch(self, tmp_path):
        """Covering less would be silent blindness; cover more instead."""
        supervisor, observer = self._supervisor(tmp_path, max_schedules=2)
        assert supervisor.degraded is False

        (tmp_path / "services").mkdir()
        (tmp_path / "services" / "deep").mkdir()
        adopted, _ = supervisor.sync_watches()

        assert supervisor.degraded is True
        assert (str(tmp_path), True) in observer.scheduled, "root now watched recursively"
        assert supervisor.watched_paths == [str(tmp_path)]
        assert adopted == [str(tmp_path / "services")]

    def test_degraded_watchers_are_reported_as_partial(self, tmp_path):
        supervisor, _ = self._supervisor(tmp_path, max_schedules=2)
        supervisor._health_path = tmp_path / "health.json"

        (tmp_path / "services").mkdir()
        supervisor.sync_watches()
        supervisor.report_health(observer_alive=True, force=True)

        health = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
        assert health["degraded"] is True
        assert watcher_status(True, {**health, "stalled": False}) == "partial"


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


class TestObserverLiveness:
    def _watched(self, tmp_path, name="lib"):
        """A supervisor with one real watch on tmp_path/<name>, plus its emitter."""
        watched = tmp_path / name
        watched.mkdir(exist_ok=True)
        observer = FakeObserver()
        supervisor = _WatchSupervisor(observer, tmp_path, [], health_path=None)
        supervisor._handler = MagicMock()
        supervisor._schedule(watched, recursive=True)
        thread, gate = _live_thread()
        observer.emitters = [FakeEmitter(thread, root=str(watched))]
        return supervisor, observer, watched, thread, gate

    def _watched_as_planned(self, tmp_path, name="src"):
        """A supervisor planned the way startup plans a repository.

        The root is watched non-recursively and each top-level directory
        recursively, which is the shape ``sync_watches`` reconciles.  The
        bare ``_watched`` fixture has no shallow parent, so a recreated
        directory there can only be noticed by the liveness check; here both
        mechanisms are in play, as they are in a real watcher.
        """
        # An ignored tree worth excluding is what makes the planner split the
        # root at all; without one it covers everything with a single
        # recursive watch and there is no shallow parent to reconcile.
        (tmp_path / "node_modules").mkdir(exist_ok=True)
        for index in range(_WATCH_SPLIT_MIN_DIRS + 2):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir(exist_ok=True)
        watched = tmp_path / name
        watched.mkdir(exist_ok=True)
        observer = FakeObserver()
        supervisor = _WatchSupervisor(
            observer, tmp_path, _load_ignore_patterns(tmp_path), health_path=None
        )
        supervisor.schedule_initial(MagicMock())
        assert (str(watched), True) in observer.scheduled
        assert str(tmp_path) in supervisor._shallow, "the root must be the shallow parent"
        thread, gate = _live_thread()
        observer.emitters = [FakeEmitter(thread, root=str(watched))]
        return supervisor, observer, watched, thread, gate

    def test_dead_backend_reader_thread_is_reported(self, tmp_path):
        """The inotify shape: the emitter lives on, its buffer thread does not."""
        supervisor, observer, watched, thread, gate = self._watched(tmp_path)

        assert supervisor.check_liveness() == ([], [])

        gate.set()
        thread.join(timeout=5)

        # The directory is untouched, so the first death buys one reschedule.
        dead, repaired = supervisor.check_liveness()
        assert dead == []
        assert repaired == [str(watched)]

        # watchdog gives the rescheduled watch a fresh emitter; if that one
        # dies too, the fault is real and has to stay loud.
        replacement, replacement_gate = _live_thread()
        observer.emitters = [FakeEmitter(replacement, root=str(watched))]
        assert supervisor.check_liveness() == ([], [])

        replacement_gate.set()
        replacement.join(timeout=5)
        dead, repaired = supervisor.check_liveness()

        assert dead == ["fake-inotify-buffer"]
        assert repaired == []

    def test_dead_emitter_thread_is_reported(self, tmp_path):
        """The Windows shape: the dispatch/emitter thread itself ends."""
        gate = threading.Event()

        class ThreadEmitter(threading.Thread):
            def __init__(self) -> None:
                super().__init__(name="fake-emitter", daemon=True)

            def run(self) -> None:
                gate.wait()

        emitter = ThreadEmitter()
        emitter.start()
        observer = FakeObserver()
        observer.emitters = [emitter]
        supervisor = _WatchSupervisor(observer, tmp_path, [], health_path=None)

        assert supervisor.check_liveness() == ([], [])

        gate.set()
        emitter.join(timeout=5)

        # No watch root to attribute it to, so there is nothing to reschedule.
        assert supervisor.check_liveness() == (["fake-emitter"], [])

    def test_unstarted_thread_is_never_mistaken_for_a_dead_one(self, tmp_path):
        """A watch scheduled mid-tick must not read as a corpse."""
        observer = FakeObserver()
        observer.emitters = [FakeEmitter(threading.Thread(target=lambda: None))]
        supervisor = _WatchSupervisor(observer, tmp_path, [], health_path=None)

        assert supervisor.check_liveness() == ([], [])
        assert supervisor.check_liveness() == ([], [])

    def test_unschedulable_observer_reports_no_deaths(self, tmp_path):
        """A mock or stub observer must not fake a death every tick."""
        supervisor = _WatchSupervisor(MagicMock(), tmp_path, [], health_path=None)

        assert supervisor.check_liveness() == ([], [])

    def test_deleting_a_watched_directory_is_not_a_death(self, tmp_path):
        """Both backends stop an emitter whose own root disappears.

        `rm -rf lib/` during normal work must not exit the watcher — with the
        daemon restarting on every exit, that is a restart loop paying for a
        full initial update each time.
        """
        supervisor, _, watched, thread, gate = self._watched(tmp_path)

        assert supervisor.check_liveness() == ([], [])

        shutil.rmtree(watched)
        gate.set()
        thread.join(timeout=5)

        assert supervisor.check_liveness() == ([], [])

    def test_recreated_directory_is_not_a_death(self, tmp_path):
        """`rm -rf src && mkdir src` inside one tick: same name, new directory.

        Calling that a death exits the watcher *and* loses the recreated
        contents, because the restarted watcher only reconciles stale rows
        away.  The watch is stale, not the watcher.

        Which mechanism notices is deliberately not asserted, because it is
        platform-dependent.  Where the inode changes -- macOS, and Linux
        whenever the filesystem does not hand it straight back --
        ``sync_watches`` releases the stale watch and adopts the replacement;
        where it does not, the once-per-root repair reschedules.  The
        guarantee is the outcome: no death, and the directory still covered by
        a watch that was scheduled after the swap.
        """
        supervisor, observer, watched, thread, gate = self._watched_as_planned(tmp_path)

        assert supervisor.check_liveness() == ([], [])
        scheduled_before = len(observer.scheduled)

        shutil.rmtree(watched)
        watched.mkdir()  # same path, new directory -- one tick, no gap
        gate.set()
        thread.join(timeout=5)

        # The watch loop syncs the watch list first, then checks liveness.
        supervisor.sync_watches()
        dead, _ = supervisor.check_liveness()

        assert dead == [], "a recreated directory was reported as a dead watcher"
        assert str(watched) in supervisor.watched_paths
        assert len(observer.scheduled) > scheduled_before, "the watch was never renewed"

    def test_recreated_directory_with_a_reused_inode_is_repaired(self, tmp_path, monkeypatch):
        """The Linux shape: the filesystem hands the same inode straight back.

        Linux has no ``st_birthtime``, so identity is blind to that swap and
        the once-per-root repair is what has to carry it.  Frozen here rather
        than left to the filesystem, so the path is pinned on every platform
        instead of only where inode reuse happens to occur.
        """
        monkeypatch.setattr(
            "code_review_graph.incremental._watch_identity", lambda path: (1, 2, 0.0)
        )
        supervisor, observer, watched, thread, gate = self._watched_as_planned(tmp_path)

        assert supervisor.check_liveness() == ([], [])
        scheduled_before = len(observer.scheduled)

        shutil.rmtree(watched)
        watched.mkdir()
        gate.set()
        thread.join(timeout=5)

        assert supervisor.sync_watches() == ([], []), "identity cannot see a reused inode"

        dead, repaired = supervisor.check_liveness()

        assert dead == []
        assert repaired == [str(watched)], "the repair path did not carry the recreate"
        assert len(observer.scheduled) > scheduled_before
        assert (str(watched), True) in observer.scheduled[scheduled_before:]

    def test_failed_reschedule_is_reported_rather_than_counted_as_repaired(self, tmp_path):
        """ENOSPC from inotify is #811's own trigger; it must not pass as a repair."""
        supervisor, observer, watched, thread, gate = self._watched(tmp_path)
        assert supervisor.check_liveness() == ([], [])

        gate.set()
        thread.join(timeout=5)
        observer.schedule = MagicMock(side_effect=OSError("No space left on device"))
        with pytest.raises(OSError, match="No space left on device"):
            supervisor.check_liveness()

        assert str(watched) not in supervisor._repaired_roots, "bookkeeping outlived the watch"

    def test_replaced_directory_is_released_and_re_adopted(self, tmp_path):
        """The identity change has to reach the watch list, not just liveness.

        The replacement is built beside the original and renamed over it, so
        the two inodes are distinct on every filesystem. Plain delete-then-
        recreate cannot be used here: Linux commonly hands the same inode back,
        which is the repair path's job, covered separately.
        """
        (tmp_path / "node_modules").mkdir()
        for index in range(6):
            (tmp_path / "node_modules" / f"pkg{index}").mkdir()
        watched = tmp_path / "src"
        watched.mkdir()
        observer = FakeObserver()
        supervisor = _WatchSupervisor(
            observer, tmp_path, _load_ignore_patterns(tmp_path), health_path=None
        )
        supervisor.schedule_initial(MagicMock())
        assert (str(watched), True) in observer.scheduled

        replacement = tmp_path / "src.incoming"
        replacement.mkdir()  # allocated while the original still holds its inode
        assert replacement.stat().st_ino != watched.stat().st_ino
        shutil.rmtree(watched)
        replacement.rename(watched)
        adopted, vanished = supervisor.sync_watches()

        assert vanished == [str(watched)], "the stale watch was not released"
        assert adopted == [str(watched)], "the replacement was not adopted"
        assert observer.scheduled.count((str(watched), True)) == 2


class TestWatchLoop:
    def _watch_with(self, tmp_path, store, observer, sleeper, health_interval=0.0, **kwargs):
        with (
            patch("watchdog.observers.Observer", return_value=observer),
            patch(
                "code_review_graph.incremental._sleep_watch_tick",
                side_effect=sleeper,
            ),
            patch(
                "code_review_graph.incremental._WATCH_HEALTH_INTERVAL",
                health_interval,
            ),
        ):
            watch(tmp_path, store, **kwargs)

    def test_dead_observer_exits_loudly_instead_of_stalling(
        self,
        tmp_path,
        caplog,
        watch_repository,
    ):
        from watchdog.observers import Observer

        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        observer = Observer()
        harness = TestRealObserver()
        with (
            caplog.at_level(logging.ERROR),
            pytest.raises(RuntimeError, match="watch observer stopped"),
        ):
            with watch_repository(repo, observer=observer) as (_reader, thread):
                assert harness._wait_for(
                    lambda: (read_watch_health(repo) or {}).get("phase") == "watching"
                ), "the watcher did not finish its initial build"
                if sys.platform.startswith("linux"):
                    from watchdog.observers.inotify_buffer import InotifyBuffer

                    def readers():
                        return [
                            value
                            for emitter in observer.emitters
                            if emitter.watch.path == str(repo)
                            for value in vars(emitter).values()
                            if isinstance(value, InotifyBuffer)
                        ]

                    assert harness._wait_for(lambda: bool(readers()))
                    first = readers()[0]
                    first.close()
                    assert harness._wait_for(
                        lambda: any(reader is not first for reader in readers())
                    ), "the failed reader was not repaired"
                    second = next(reader for reader in readers() if reader is not first)
                    second.close()
                    failed_name = second.name
                else:
                    observer.stop()
                    failed_name = observer.name
                thread.join(timeout=20)
                assert not thread.is_alive()

        assert str(repo) in caplog.text
        health = read_watch_health(repo)
        assert health is not None
        assert health["observer_alive"] is False
        assert health["stalled"] is True
        assert failed_name in health["dead_threads"]

    def test_cli_watch_turns_a_dead_observer_into_exit_code_1(self):
        """The daemon restarts on process exit, so the exit code has to be non-zero."""
        from code_review_graph import cli

        argv = ["code-review-graph", "watch", "--repo", "repo-root"]
        with (
            patch.object(sys, "argv", argv),
            patch("code_review_graph.graph.GraphStore", return_value=MagicMock()),
            patch("code_review_graph.incremental.get_db_path", return_value=MagicMock()),
            patch(
                "code_review_graph.incremental.watch",
                side_effect=RuntimeError("watch observer stopped: dead thread(s) Thread-3"),
            ),
            pytest.raises(SystemExit) as exit_info,
        ):
            cli.main()

        assert exit_info.value.code == 1

    def test_live_observer_publishes_health_for_daemon_status(self, tmp_path, watch_repository):
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        observer = TestRealObserver()
        with watch_repository(repo) as (_reader, thread):
            assert observer._wait_for(
                lambda: (read_watch_health(repo) or {}).get("phase") == "watching"
            ), "the watcher did not finish its initial build"
            health = read_watch_health(repo)
            assert health is not None
            assert health["observer_alive"] is True
            assert health["stalled"] is False
            assert health["watched_paths"] >= 1
            assert thread.is_alive()
        assert not watch_health_path(repo).exists()

    def test_normal_file_change_still_updates_the_graph(self, tmp_path, watch_repository):
        """A real observer indexes the change and excludes ignored directories."""
        repo = tmp_path / "repo"
        source_dir = repo / "src"
        source_dir.mkdir(parents=True)
        for index in range(6):
            (repo / "node_modules" / f"pkg{index}").mkdir(parents=True)
        source = source_dir / "app.py"
        ignored = repo / "node_modules" / "pkg0" / "ignored.py"
        callbacks: list[int] = []
        observer = TestRealObserver()
        with watch_repository(repo, lambda _store: callbacks.append(1)) as (reader, thread):
            assert observer._wait_for(
                lambda: (read_watch_health(repo) or {}).get("phase") == "watching"
            ), "the watcher did not finish its initial build"
            initial_callbacks = len(callbacks)
            source.write_text("def handler():\n    return 1\n", encoding="utf-8")
            ignored.write_text("def ignored():\n    return 1\n", encoding="utf-8")
            assert observer._wait_for(lambda: bool(reader.get_nodes_by_file(str(source))))
            assert observer._wait_for(lambda: len(callbacks) > initial_callbacks)
            assert observer._wait_for(
                lambda: bool((read_watch_health(repo) or {}).get("events_seen"))
            ), "the watcher did not publish event health"
            health = read_watch_health(repo)
            assert health is not None and health["observer_alive"] is True
            assert isinstance(health["last_event_at"], float)
            assert thread.is_alive()
            assert ignored.as_posix() not in reader.get_all_files()

    def test_relative_repo_keeps_a_graph_built_with_an_absolute_root(self, tmp_path):
        """A real watcher using ``.`` preserves the absolute-root graph."""
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        source = repo / "src" / "app.py"
        source.write_text("def handler():\n    return 1\n", encoding="utf-8")
        database = tmp_path / "graph.db"
        with GraphStore(database) as store:
            incremental_update(repo, store, changed_files=["src/app.py"])
            before = store.get_all_files()
        assert before == [source.as_posix()]
        command = (
            "from pathlib import Path\n"
            "from threading import Event\n"
            "from code_review_graph.graph import GraphStore\n"
            "from code_review_graph.incremental import watch\n"
            "import sys\n"
            "stop = Event()\n"
            "stop.set()\n"
            "with GraphStore(Path(sys.argv[1])) as store:\n"
            "    watch(Path('.'), store, stop_event=stop)\n"
        )
        subprocess.run(
            [sys.executable, "-c", command, str(database)],
            cwd=repo,
            check=True,
            timeout=30,
        )
        with GraphStore(database) as store:
            assert store.get_all_files() == before

    def test_a_graph_from_another_root_is_refused_not_emptied(self, tmp_path):
        """A graph built from a different real repository is never rewritten."""
        repo = tmp_path / "repo"
        other = tmp_path / "other"
        repo.mkdir()
        other.mkdir()
        source = other / "module.py"
        source.write_text("def handler():\n    return 1\n", encoding="utf-8")
        with GraphStore(tmp_path / "graph.db") as store:
            incremental_update(other, store, changed_files=[str(source.relative_to(other))])
            original_files = store.get_all_files()
            assert original_files == [source.as_posix()]
            with pytest.raises(RuntimeError, match="different repository root"):
                watch(repo, store)
            assert store.get_all_files() == original_files
            assert not watch_health_path(repo).exists()

    def test_repair_reconciles_files_deleted_during_the_outage(self, tmp_path, watch_repository):
        """A real stopped emitter must reconcile both deleted and new files."""
        from watchdog.observers import Observer

        repo = tmp_path / "repo"
        source_dir = repo / "src"
        source_dir.mkdir(parents=True)
        for index in range(6):
            (repo / "node_modules" / f"pkg{index}").mkdir(parents=True)
        doomed = source_dir / "doomed.py"
        keeper = source_dir / "keeper.py"
        arrived = source_dir / "arrived.py"
        doomed.write_text("def doomed():\n    return 1\n", encoding="utf-8")
        keeper.write_text("def keeper():\n    return 2\n", encoding="utf-8")
        observer = Observer()
        harness = TestRealObserver()
        with watch_repository(repo, observer=observer) as (reader, thread):
            assert harness._wait_for(
                lambda: (read_watch_health(repo) or {}).get("phase") == "watching"
            ), "the watcher did not finish its initial build"
            assert doomed.as_posix() in reader.get_all_files()
            emitter = next(
                emitter for emitter in observer.emitters if emitter.watch.path == str(source_dir)
            )
            emitter.stop()
            doomed.unlink()
            arrived.write_text("def arrived():\n    return 3\n", encoding="utf-8")
            assert harness._wait_for(
                lambda: (
                    arrived.as_posix() in reader.get_all_files()
                    and doomed.as_posix() not in reader.get_all_files()
                )
            ), "the repaired watch did not reconcile the outage"
            assert keeper.as_posix() in reader.get_all_files()
            assert thread.is_alive()

    def test_health_is_published_before_the_first_build(self, tmp_path):
        """A real blocked graph write cannot hide the initial-build heartbeat."""
        import sqlite3
        from concurrent.futures import ThreadPoolExecutor

        repo = tmp_path / "repo"
        source = repo / "src"
        source.mkdir(parents=True)
        (source / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
        database = repo / "graph.db"
        stop = threading.Event()
        with GraphStore(database) as store, sqlite3.connect(database) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(watch, repo, store, stop_event=stop)
                try:
                    assert TestRealObserver()._wait_for(
                        lambda: (read_watch_health(repo) or {}).get("phase") == "initial-build"
                    ), "the initial build did not expose its health"
                    health = read_watch_health(repo)
                    assert health is not None and health["stalled"] is False
                finally:
                    blocker.rollback()
                    stop.set()
                future.result(timeout=20)

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="os.kill(SIGTERM) terminates Windows processes before the handler can run",
    )
    def test_sigterm_clears_the_health_file(self, tmp_path):
        """The native SIGTERM handler clears health and restores the handler."""
        import signal

        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        command = (
            "import signal, sys\n"
            "from pathlib import Path\n"
            "from code_review_graph.graph import GraphStore\n"
            "from code_review_graph.incremental import watch\n"
            "repo = Path(sys.argv[1])\n"
            'with GraphStore(repo / "graph.db") as store:\n'
            "    watch(repo, store)\n"
            "print(signal.getsignal(signal.SIGTERM) == signal.SIG_DFL)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", command, str(repo)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert TestRealObserver()._wait_for(
                lambda: (read_watch_health(repo) or {}).get("phase") == "watching"
            ), "the watcher did not finish its initial build"
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=20)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
        assert process.returncode == 0, stderr
        assert stdout.strip() == "True"
        assert not watch_health_path(repo).exists()

    def test_health_is_not_rewritten_on_every_tick(self, tmp_path):
        """Real watch ticks respect the heartbeat interval but failures do not."""
        import os

        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        for index in range(6):
            (repo / "node_modules" / f"pkg{index}").mkdir(parents=True)
        command = (
            "import sys, threading\n"
            "from pathlib import Path\n"
            "from watchdog.observers import Observer\n"
            "from code_review_graph.graph import GraphStore\n"
            "from code_review_graph.incremental import watch\n"
            "repo = Path(sys.argv[1])\n"
            "observer = Observer()\n"
            "def control():\n"
            "    sys.stdin.readline()\n"
            "    observer.stop()\n"
            "threading.Thread(target=control, daemon=True).start()\n"
            'with GraphStore(repo / "graph.db") as store:\n'
            "    watch(repo, store, observer=observer)\n"
        )
        environment = dict(os.environ, CRG_WATCH_HEALTH_INTERVAL="3600")
        process = subprocess.Popen(
            [sys.executable, "-c", command, str(repo)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            assert TestRealObserver()._wait_for(
                lambda: (read_watch_health(repo) or {}).get("phase") == "watching"
            ), "the watcher did not finish its initial build"
            health_path = watch_health_path(repo)
            before = health_path.read_bytes()
            service = repo / "services"
            service.mkdir()
            source = service / "app.py"
            source.write_text("def handler():\n    return 1\n", encoding="utf-8")
            with GraphStore(repo / "graph.db") as reader:
                assert TestRealObserver()._wait_for(
                    lambda: source.as_posix() in reader.get_all_files()
                ), "the watcher did not complete an adoption tick"
            assert health_path.read_bytes() == before
            assert process.stdin is not None
            process.stdin.write("stop\n")
            process.stdin.flush()
            _stdout, stderr = process.communicate(timeout=20)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
        assert process.returncode != 0
        assert "watch observer stopped" in stderr
        health = read_watch_health(repo)
        assert health is not None and health["stalled"] is True
        assert health_path.read_bytes() != before


class TestRealObserver:
    """The one place a fake observer cannot be trusted.

    Backend semantics differ: on macOS a non-recursive watch delivers no
    directory event at all for a child, and no file event from inside it
    (``FSEventsEmitter._is_recursive_event``).  Any design that learns about
    new directories from events is silently blind there, so this drives the
    real ``Observer`` and asserts the file reaches the graph.
    """

    def _wait_for(self, predicate, timeout=20.0, interval=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def test_write_during_initial_postprocessing_reaches_graph(self, tmp_path, watch_repository):
        repo = tmp_path / "repo"
        repo.mkdir()
        source = repo / "app.py"
        source.write_text("def original():\n    return 1\n", encoding="utf-8")
        changed = threading.Event()

        def postprocess(_store):
            if not changed.is_set():
                source.write_text("def replacement():\n    return 2\n", encoding="utf-8")
                changed.set()
                # Real observer events arrive while initial derived-state work
                # is still running, before the watcher publishes readiness.
                time.sleep(0.2)

        with watch_repository(repo, postprocess) as (reader, thread):
            assert changed.wait(20), "initial postprocessing never ran"
            indexed = self._wait_for(
                lambda: any(
                    node.name == "replacement" for node in reader.get_nodes_by_file(str(source))
                ),
                timeout=5,
            )

        assert indexed, "a write during initial postprocessing stayed unindexed"

    def test_clean_stop_publishes_a_pending_source_change(self, tmp_path, watch_repository):
        repo = tmp_path / "repo"
        repo.mkdir()
        source = repo / "app.py"
        source.write_text("def original():\n    return 1\n", encoding="utf-8")
        changed = threading.Event()

        def postprocess(_store):
            if not changed.is_set():
                source.write_text("def replacement():\n    return 2\n", encoding="utf-8")
                time.sleep(0.2)
                changed.set()

        with watch_repository(repo, postprocess):
            assert changed.wait(20), "initial postprocessing never ran"

        with GraphStore(repo / "graph.db") as persisted:
            names = {node.name for node in persisted.get_nodes_by_file(str(source))}
        assert "replacement" in names, "shutdown discarded the accepted source change"
        assert "original" not in names

    def test_clean_stop_propagates_a_pending_callback_failure(self, tmp_path, watch_repository):
        repo = tmp_path / "repo"
        repo.mkdir()
        source = repo / "app.py"
        source.write_text("def original():\n    return 1\n", encoding="utf-8")
        changed = threading.Event()
        failure = ValueError("postprocessing refused the changed source")

        def postprocess(_store):
            if changed.is_set():
                raise failure
            source.write_text("def replacement():\n    return 2\n", encoding="utf-8")
            time.sleep(0.2)
            changed.set()

        with pytest.raises(RuntimeError, match="watch update failed") as caught:
            with watch_repository(repo, postprocess):
                assert changed.wait(20), "initial postprocessing never ran"
        assert caught.value.__cause__ is failure

    def test_continuous_source_changes_publish_before_the_writer_stops(
        self,
        tmp_path,
        watch_repository,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        source = repo / "app.py"
        source.write_text("def version():\n    return 0\n", encoding="utf-8")
        ready = threading.Event()
        published = threading.Event()
        stop_writer = threading.Event()

        def postprocess(_store):
            if ready.is_set():
                published.set()
            else:
                ready.set()

        def write_sources():
            version = 1
            # In place: Windows denies replacing a file the watcher has open.
            while not stop_writer.is_set():
                source.write_text(f"def version():\n    return {version}\n", encoding="utf-8")
                version += 1
                stop_writer.wait(0.02)

        with watch_repository(repo, postprocess) as (reader, _thread):
            assert ready.wait(20), "initial postprocessing never ran"
            original_hash = reader.get_nodes_by_file(str(source))[0].file_hash
            writer = threading.Thread(target=write_sources, name="source-writer", daemon=True)
            writer.start()
            try:
                assert published.wait(5), "continuous events postponed publication indefinitely"
                assert writer.is_alive()
                assert any(
                    node.file_hash != original_hash
                    for node in reader.get_nodes_by_file(str(source))
                )
            finally:
                stop_writer.set()
                writer.join(timeout=20)
                assert not writer.is_alive()

    def test_real_observer_teardown_timeout_is_a_failure(self, tmp_path):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        from code_review_graph.incremental import _run_time_boxed

        entered = threading.Event()
        release = threading.Event()
        source = tmp_path / "blocked.py"

        class BlockingHandler(FileSystemEventHandler):
            def on_created(self, event):
                if event.src_path == str(source):
                    entered.set()
                    release.wait(20)

        observer = Observer()
        observer.schedule(BlockingHandler(), str(tmp_path), recursive=False)
        observer.start()
        try:
            source.write_text("def blocked():\n    return 1\n", encoding="utf-8")
            assert entered.wait(20), "the real observer never dispatched the source event"
            with pytest.raises(TimeoutError, match="observer stop"):
                _run_time_boxed(observer.stop, "observer stop", timeout=0.05)
        finally:
            release.set()
            observer.stop()
            observer.join(timeout=20)
            assert not observer.is_alive()

    def test_teardown_propagates_the_real_operation_failure(self, tmp_path):
        from code_review_graph.incremental import _run_time_boxed

        missing = tmp_path / "already-removed.txt"
        with pytest.raises(FileNotFoundError) as caught:
            _run_time_boxed(missing.unlink, "remove unavailable file", timeout=1)
        assert caught.value.filename == str(missing)

    def test_new_top_level_directory_is_indexed(self, tmp_path, watch_repository):
        from watchdog.observers import Observer

        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        for index in range(6):
            (repo / "node_modules" / f"pkg{index}").mkdir(parents=True)
        (repo / "src" / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "src/app.py"], cwd=repo, check=True)

        observer = Observer()
        with watch_repository(repo, observer=observer) as (reader, thread):
            assert self._wait_for(
                lambda: (read_watch_health(repo) or {}).get("phase") == "watching"
            ), "the watcher did not finish its initial build"
            assert any(
                emitter.watch.path == str(repo) and not emitter.watch.is_recursive
                for emitter in observer.emitters
            ), "the repository root was not watched shallowly"
            ignored = repo / "dist"
            ignored.mkdir()
            ignored_file = ignored / "ignored.py"
            ignored_file.write_text("def ignored():\n    return 0\n", encoding="utf-8")
            created = repo / "services"
            assert all(emitter.watch.path != str(created) for emitter in observer.emitters)
            created.mkdir()
            (created / "svc.py").write_text("def service():\n    return 3\n", encoding="utf-8")
            subprocess.run(["git", "add", "services/svc.py"], cwd=repo, check=True)
            assert self._wait_for(
                lambda: any(
                    emitter.watch.path == str(created) and emitter.watch.is_recursive
                    for emitter in observer.emitters
                )
            ), "the new directory did not receive a recursive watch"
            indexed = (created / "svc.py").as_posix()
            assert self._wait_for(lambda: indexed in reader.get_all_files()), (
                "the new directory was not indexed"
            )
            assert all(emitter.watch.path != str(ignored) for emitter in observer.emitters)
            assert ignored_file.as_posix() not in reader.get_all_files()
            nested = repo / "src" / "nested"
            assert any(
                emitter.watch.path == str(repo / "src") and emitter.watch.is_recursive
                for emitter in observer.emitters
            ), "the parent directory lacks its recursive watch"
            nested.mkdir()
            nested_file = nested / "child.py"
            nested_file.write_text("def child():\n    return 4\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/nested/child.py"], cwd=repo, check=True)
            assert self._wait_for(lambda: nested_file.as_posix() in reader.get_all_files())
            assert all(emitter.watch.path != str(nested) for emitter in observer.emitters)
            assert thread.is_alive()

    def test_recreated_directory_survives_and_is_reindexed(self, tmp_path, watch_repository):
        """Re-adopt a directory after separate and same-tick replacement."""
        from watchdog.observers import Observer

        repo = tmp_path / "repo"
        source = repo / "src"
        source.mkdir(parents=True)
        for index in range(6):
            (repo / "node_modules" / f"pkg{index}").mkdir(parents=True)
        original = source / "app.py"
        original.write_text("def handler():\n    return 1\n", encoding="utf-8")
        observer = Observer()
        with watch_repository(repo, observer=observer) as (reader, thread):
            assert self._wait_for(
                lambda: (read_watch_health(repo) or {}).get("phase") == "watching"
            )
            first = next(
                emitter for emitter in observer.emitters if emitter.watch.path == str(source)
            )
            assert original.as_posix() in reader.get_all_files()
            shutil.rmtree(source)
            assert self._wait_for(
                lambda: (
                    all(emitter.watch.path != str(source) for emitter in observer.emitters)
                    and original.as_posix() not in reader.get_all_files()
                )
            ), "the removed directory was not released"
            assert thread.is_alive()
            source.mkdir()
            restored = source / "restored.py"
            restored.write_text("def restored():\n    return 2\n", encoding="utf-8")
            assert self._wait_for(
                lambda: (
                    any(
                        emitter is not first and emitter.watch.path == str(source)
                        for emitter in observer.emitters
                    )
                    and restored.as_posix() in reader.get_all_files()
                )
            ), "the restored directory was not re-adopted"
            shutil.rmtree(source)
            source.mkdir()
            reborn = source / "reborn.py"
            reborn.write_text("def reborn():\n    return 5\n", encoding="utf-8")
            assert self._wait_for(
                lambda: (
                    reborn.as_posix() in reader.get_all_files()
                    and restored.as_posix() not in reader.get_all_files()
                )
            ), "the same-tick replacement was not reindexed"
            assert thread.is_alive()

    def test_deleting_a_watched_directory_does_not_kill_the_watcher(
        self,
        tmp_path,
        watch_repository,
    ):
        """A deleted directory loses its emitter and graph rows without a crash."""
        from watchdog.observers import Observer

        repo = tmp_path / "repo"
        library = repo / "lib"
        library.mkdir(parents=True)
        for index in range(6):
            (repo / "node_modules" / f"pkg{index}").mkdir(parents=True)
        (library / "mod.py").write_text("def mod():\n    return 1\n", encoding="utf-8")
        observer = Observer()
        with watch_repository(repo, observer=observer) as (reader, thread):
            assert self._wait_for(
                lambda: (read_watch_health(repo) or {}).get("phase") == "watching"
            )
            assert any(emitter.watch.path == str(library) for emitter in observer.emitters)
            assert reader.get_all_files()
            shutil.rmtree(library)
            assert self._wait_for(
                lambda: (
                    all(emitter.watch.path != str(library) for emitter in observer.emitters)
                    and not reader.get_all_files()
                )
            ), "the removed directory retained its watch or graph rows"
            assert thread.is_alive()


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------


def _write_health(repo_root: Path, **fields) -> None:
    payload = {
        "repo": str(repo_root),
        "pid": 4242,
        "started_at": time.time(),
        "updated_at": time.time(),
        "observer_alive": True,
        "last_event_at": time.time(),
        "events_seen": 3,
        "watched_paths": 4,
        "dead_threads": [],
    }
    payload.update(fields)
    path = watch_health_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestStatusSurfacesStalls:
    def test_missing_health_reads_as_unknown(self, tmp_path):
        assert read_watch_health(tmp_path) is None
        assert watcher_status(True, None) == "unknown"

    def test_corrupt_health_file_is_ignored(self, tmp_path):
        path = watch_health_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        assert read_watch_health(tmp_path) is None

    def test_dead_observer_reads_as_stalled(self, tmp_path):
        _write_health(tmp_path, observer_alive=False, dead_threads=["Thread-3"])

        health = read_watch_health(tmp_path)

        assert health is not None
        assert health["stalled"] is True
        assert watcher_status(True, health) == "stalled"

    def test_frozen_heartbeat_reads_as_stalled(self, tmp_path):
        _write_health(tmp_path, updated_at=time.time() - 600)

        health = read_watch_health(tmp_path)

        assert health is not None
        assert health["stalled"] is True
        assert health["age"] > 500

    def test_fresh_heartbeat_reads_as_ok(self, tmp_path):
        _write_health(tmp_path)

        health = read_watch_health(tmp_path)

        assert health is not None
        assert health["stalled"] is False
        assert watcher_status(True, health) == "ok"

    def test_daemon_status_reports_the_stall(self, tmp_path):
        from code_review_graph.daemon import WatchDaemon

        repo = tmp_path / "repo"
        repo.mkdir()
        _write_health(repo, observer_alive=False, dead_threads=["Thread-3"])
        config = DaemonConfig(
            repos=[WatchRepo(path=str(repo), alias="repo")],
            log_dir=tmp_path / "logs",
        )
        daemon = WatchDaemon(config=config, config_path=tmp_path / "watch.toml")

        with (
            patch(
                "code_review_graph.daemon.load_state",
                return_value={"repo": {"pid": 4242, "path": str(repo)}},
            ),
            patch("code_review_graph.daemon._is_pid_alive", return_value=True),
        ):
            entry = daemon.status()["repos"][0]

        assert entry["alive"] is True, "the process really is still running"
        assert entry["watcher"] == "stalled"
        assert entry["observer_alive"] is False

    def test_daemon_cli_status_prints_a_stalled_watcher(self, tmp_path):
        from code_review_graph.daemon_cli import _handle_status

        repo = tmp_path / "repo"
        repo.mkdir()
        _write_health(repo, observer_alive=False, dead_threads=["Thread-3"])
        config = DaemonConfig(
            repos=[WatchRepo(path=str(repo), alias="repo")],
            log_dir=tmp_path / "logs",
        )

        with (
            patch("code_review_graph.daemon.is_daemon_running", return_value=True),
            patch("code_review_graph.daemon.load_config", return_value=config),
            patch("code_review_graph.daemon.read_pid", return_value=4242),
            patch(
                "code_review_graph.daemon.load_state",
                return_value={"repo": {"pid": 4242, "path": str(repo)}},
            ),
            patch("code_review_graph.daemon.pid_alive", return_value=True),
            patch("builtins.print") as printer,
        ):
            _handle_status(MagicMock())

        printed = "\n".join(str(call) for call in printer.call_args_list)
        assert "stalled" in printed
        assert "alive" in printed, "the process column still says alive"

    def test_restart_backs_off_instead_of_looping(self, tmp_path, caplog):
        """A watcher that keeps dying must not repay a full build every 30s."""
        from code_review_graph.daemon import WatchDaemon

        repo = tmp_path / "repo"
        repo.mkdir()
        config = DaemonConfig(
            repos=[WatchRepo(path=str(repo), alias="repo")],
            log_dir=tmp_path / "logs",
        )
        daemon = WatchDaemon(config=config, config_path=tmp_path / "watch.toml")
        daemon._current_repos = {"repo": config.repos[0]}
        dead = MagicMock()
        dead.poll.return_value = 1

        with patch.object(WatchDaemon, "_start_watcher") as restart:
            daemon._children = {"repo": dead}
            daemon._check_health()
            assert restart.call_count == 1, "the first death restarts immediately"
            assert daemon.restart_count("repo") == 1

            daemon._children = {"repo": dead}
            daemon._check_health()
            assert restart.call_count == 1, "the second death inside the window waits"
            assert daemon.restart_count("repo") == 1

            daemon._restarts["repo"]["next_attempt"] = 0.0  # window elapsed
            daemon._children = {"repo": dead}
            daemon._check_health()
            assert restart.call_count == 2
            assert daemon.restart_count("repo") == 2

    def test_restart_counter_resets_after_a_healthy_run(self, tmp_path):
        from code_review_graph.daemon import WatchDaemon

        repo = tmp_path / "repo"
        repo.mkdir()
        config = DaemonConfig(
            repos=[WatchRepo(path=str(repo), alias="repo")],
            log_dir=tmp_path / "logs",
        )
        daemon = WatchDaemon(config=config, config_path=tmp_path / "watch.toml")
        daemon._current_repos = {"repo": config.repos[0]}
        daemon._restarts["repo"] = {
            "count": 5,
            "next_attempt": 0.0,
            "started_at": time.monotonic() - 100000,
        }
        dead = MagicMock()
        dead.poll.return_value = 1

        with patch.object(WatchDaemon, "_start_watcher"):
            daemon._children = {"repo": dead}
            daemon._check_health()

        assert daemon.restart_count("repo") == 1, "a long healthy run clears the history"

    def test_reaping_a_child_clears_its_health_file(self, tmp_path):
        """A killed child cannot clear its own file; the leftover reads as stalled."""
        from code_review_graph.daemon import WatchDaemon

        repo = tmp_path / "repo"
        repo.mkdir()
        _write_health(repo)
        assert watch_health_path(repo).exists()

        proc = MagicMock()
        proc.poll.return_value = 0
        WatchDaemon._terminate_child("repo", proc, str(repo))

        assert not watch_health_path(repo).exists()

    def test_daemon_health_check_warns_about_a_stalled_watcher(self, tmp_path, caplog):
        from code_review_graph.daemon import WatchDaemon

        repo = tmp_path / "repo"
        repo.mkdir()
        _write_health(repo, observer_alive=False)
        config = DaemonConfig(
            repos=[WatchRepo(path=str(repo), alias="repo")],
            log_dir=tmp_path / "logs",
        )
        daemon = WatchDaemon(config=config, config_path=tmp_path / "watch.toml")
        child = MagicMock()
        child.poll.return_value = None
        daemon._current_repos = {"repo": config.repos[0]}
        daemon._children = {"repo": child}

        with caplog.at_level(logging.WARNING):
            with patch.object(WatchDaemon, "_start_watcher") as restart:
                daemon._check_health()

        assert "stalled" in caplog.text
        restart.assert_not_called()
