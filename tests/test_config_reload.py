"""Native daemon config watcher accepts files published by atomic rename."""

import errno
import threading

import pytest

from code_review_graph.daemon import ConfigWatcher


@pytest.mark.parametrize("change", ["replace", "move_away", "delete"])
def test_config_path_changes_trigger_reload(tmp_path, change):
    config = tmp_path / "watch.toml"
    config.write_text('[daemon]\nsession_name = "old"\n', encoding="utf-8")
    reloaded = threading.Event()
    watcher = ConfigWatcher(config, reloaded.set)
    watcher.start()
    try:
        candidate = config.with_suffix(".new")
        if change == "replace":
            candidate.write_text('[daemon]\nsession_name = "new"\n', encoding="utf-8")
            candidate.replace(config)
        elif change == "move_away":
            config.replace(candidate)
        else:
            config.unlink()
        assert reloaded.wait(5), f"the watcher did not see {change}"
    finally:
        watcher.stop()


def test_exhausted_notification_quota_fails_loud_instead_of_polling(tmp_path, monkeypatch):
    def exhausted():
        raise OSError(errno.ENOSPC, "watch quota exhausted")

    monkeypatch.setattr("watchdog.observers.Observer", exhausted)
    config = tmp_path / "watch.toml"
    config.write_text("[daemon]\n", encoding="utf-8")
    watcher = ConfigWatcher(config, threading.Event().set)
    with pytest.raises(OSError, match="watch quota exhausted"):
        watcher.start()
    assert not watcher.is_alive()


def test_polling_only_backend_fails_loud(tmp_path, monkeypatch):
    from watchdog.observers.polling import PollingObserver

    monkeypatch.setattr("watchdog.observers.Observer", PollingObserver)
    config = tmp_path / "watch.toml"
    config.write_text("[daemon]\n", encoding="utf-8")
    watcher = ConfigWatcher(config, threading.Event().set)
    with pytest.raises(RuntimeError, match="polling is not supported"):
        watcher.start()
    assert not watcher.is_alive()
