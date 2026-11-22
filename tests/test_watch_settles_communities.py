"""A watched edit costs work proportional to the change (aihub-sdf92).

Every watch batch used to rerun whole-graph community detection (Leiden) and
an FTS pass over the whole table, so one saved file cost as much as a
rebuild. A batch now rewrites only the edited file's FTS rows and leaves
community detection to one pass once the repository has been quiet.
"""

from __future__ import annotations

import subprocess
import threading
import time

from code_review_graph import communities, flows, incremental
from code_review_graph.daemon import read_watch_health
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, watch
from code_review_graph.postprocessing import run_post_processing

_GIT = ["git", "-c", "user.email=t@e", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
_SETTLE_SECONDS = 2.0


def _wait_for(condition, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def test_one_file_edit_defers_leiden_to_one_quiet_pass(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    for index in range(3):
        (repo / f"mod{index}.py").write_text(
            f"def helper{index}():\n    return {index}\n\n"
            f"def caller{index}():\n    return helper{index}()\n",
            encoding="utf-8",
        )
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run([*_GIT, *args], cwd=repo, check=True, capture_output=True)

    store = GraphStore(tmp_path / "graph.db")
    full_build(repo, store)
    run_post_processing(store)

    leiden_passes: list[float] = []
    real_detect = communities.detect_communities

    def counting_detect(*args, **kwargs):
        leiden_passes.append(time.monotonic())
        return real_detect(*args, **kwargs)

    monkeypatch.setattr(communities, "detect_communities", counting_detect)
    monkeypatch.setattr(incremental, "_SETTLE_SECONDS", _SETTLE_SECONDS)
    full_flows = []
    real_trace = flows.trace_flows

    def count_full_flows(*args, **kwargs):
        full_flows.append(1)
        return real_trace(*args, **kwargs)

    monkeypatch.setattr(flows, "trace_flows", count_full_flows)
    fts_modes: list[str] = []
    real_post_processing = run_post_processing

    def recording_post_processing(graph, **kwargs):
        result = real_post_processing(graph, **kwargs)
        fts_modes.append(result.get("fts_mode", ""))
        return result

    stop = threading.Event()
    thread = threading.Thread(
        target=watch,
        args=(repo, store),
        kwargs={"on_files_updated": recording_post_processing, "stop_event": stop},
        daemon=True,
    )
    thread.start()
    reader = GraphStore(tmp_path / "graph.db")
    try:
        assert _wait_for(
            lambda: (read_watch_health(str(repo)) or {}).get("phase") == "watching"
        ), "the watcher never started"
        (repo / "mod1.py").write_text(
            "def helper1():\n    return 1\n\ndef caller1():\n    return helper1()\n\n"
            "def addedLater():\n    return 2\n",
            encoding="utf-8",
        )

        def edited() -> bool:
            names = reader._conn.execute(
                "SELECT name FROM nodes WHERE name = 'addedLater'"
            ).fetchall()
            return bool(names)

        assert _wait_for(edited), "the edit never reached the graph"
        assert _wait_for(lambda: bool(fts_modes)), "the batch was never post-processed"
        edited_at = time.monotonic()
        assert leiden_passes == [], "a one-file edit ran a full Leiden pass"
        assert fts_modes == ["delta"], fts_modes

        assert _wait_for(lambda: len(leiden_passes) == 1), "the quiet pass never ran"
        assert leiden_passes[0] - edited_at >= _SETTLE_SECONDS - 1.0
        time.sleep(_SETTLE_SECONDS + 1.0)
        assert len(leiden_passes) == 1, "an idle watcher kept rerunning Leiden"
        assert fts_modes == ["delta", ""], fts_modes
        assert full_flows == []
    finally:
        stop.set()
        thread.join(timeout=20)
        reader.close()
        store.close()
    assert not thread.is_alive()
