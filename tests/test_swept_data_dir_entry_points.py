"""Entry points refuse a registered graph directory that was swept away.

The checkout-local graph that predates a relocation is stale, so no entry
point may answer from it: the PreToolUse enrichment hook adds no context,
agent-facing tools and CLI commands fail naming the dead registry entry, and
none of them recreates the swept directory.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from code_review_graph import cli
from code_review_graph.errors import GraphStoreError
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.registry import Registry
from code_review_graph.search import rebuild_fts_index

_GIT = [
    "git", "-c", "user.email=test@example.com", "-c", "user.name=test",
    "-c", "commit.gpgsign=false",
]


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv("CRG_REPO_ROOT", raising=False)
    monkeypatch.delenv("CRG_SHARED_DB", raising=False)


def _make_repo(base: Path) -> Path:
    repo = base / "repo"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text(
        "def helper(x):\n    return x + 1\n\n\ndef main():\n    return helper(2)\n",
        encoding="utf-8",
    )
    for args in (["init", "-q"], ["add", "app.py"], ["commit", "-qm", "init"]):
        subprocess.run(
            [*_GIT, "-C", str(repo), *args],
            check=True, capture_output=True, timeout=30,
        )
    return repo


@pytest.fixture
def swept_repo(tmp_path):
    """A repo with a built local graph whose registered data dir is gone."""
    repo = _make_repo(tmp_path)
    db = repo / ".code-review-graph" / "graph.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = GraphStore(db)
    try:
        full_build(repo, store)
        rebuild_fts_index(store)
    finally:
        store.close()
    swept = tmp_path / "swept" / "data"
    Registry().set_data_dir(str(repo), str(swept))
    return repo, db, swept


@pytest.mark.parametrize("tool", ["Grep", "Read"])
def test_pretooluse_hook_adds_no_stale_context(swept_repo, monkeypatch, capsys, caplog, tool):
    from code_review_graph.enrich import run_hook

    repo, _db, swept = swept_repo
    tool_input = (
        {"pattern": "helper"} if tool == "Grep"
        else {"file_path": str(repo / "app.py")}
    )
    payload = {"tool_name": tool, "tool_input": tool_input, "cwd": str(repo)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    run_hook()

    assert capsys.readouterr().out == ""
    assert str(swept) in caplog.text
    assert not swept.exists()


def test_get_minimal_context_names_the_dead_entry(swept_repo):
    from code_review_graph.tools.context import get_minimal_context

    repo, _db, swept = swept_repo

    with pytest.raises(GraphStoreError, match="prune --apply"):
        get_minimal_context(task="review", repo_root=str(repo))
    assert not swept.exists()


@pytest.mark.parametrize(
    "argv",
    [["search", "helper"], ["visualize", "--format", "json"], ["status"]],
)
def test_cli_commands_fail_naming_the_dead_entry(swept_repo, capsys, argv):
    repo, _db, swept = swept_repo

    with (
        patch.object(sys, "argv", ["code-review-graph", *argv, "--repo", str(repo)]),
        pytest.raises(SystemExit) as exited,
    ):
        cli.main()

    assert exited.value.code == 1
    assert str(swept) in capsys.readouterr().err
    assert not swept.exists()
