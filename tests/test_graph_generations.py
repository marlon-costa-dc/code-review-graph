import sqlite3
from contextlib import closing

import pytest

from code_review_graph.graph_generations import (
    content_identity,
    ensure_generation,
    initialize_bindings,
    publish_binding,
    read_binding,
)


def test_shared_generation_binding_is_atomic_and_rejects_stale_publication(tmp_path):
    db = tmp_path / "shared.db"
    with (
        closing(sqlite3.connect(db, isolation_level=None)) as writer,
        closing(sqlite3.connect(db, isolation_level=None)) as reader,
    ):
        writer.execute("PRAGMA journal_mode=WAL")
        initialize_bindings(writer)
        writer.execute("BEGIN IMMEDIATE")
        assert ensure_generation(writer, "repo", "clean", "profile") == 1
        assert ensure_generation(writer, "repo", "clean", "profile") == 1
        assert publish_binding(writer, "left", 1, expected_revision=0)
        assert publish_binding(writer, "right", 1, expected_revision=0)
        assert read_binding(reader, "right") is None
        writer.commit()

        reader.execute("BEGIN")
        assert read_binding(reader, "right") == (1, 1)
        writer.execute("BEGIN IMMEDIATE")
        assert ensure_generation(writer, "repo", "dirty", "profile") == 2
        assert publish_binding(writer, "right", 2, expected_revision=1)
        writer.commit()
        assert read_binding(reader, "right") == (1, 1)
        reader.commit()
        assert read_binding(reader, "right") == (2, 2)
        assert read_binding(reader, "left") == (1, 1)

        writer.execute("BEGIN IMMEDIATE")
        assert publish_binding(writer, "right", 1, expected_revision=2)
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        assert not publish_binding(writer, "right", 3, expected_revision=1)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            publish_binding(writer, "right", 4, expected_revision=3)
        candidate = ensure_generation(writer, "repo", "clean", "different-profile")
        assert candidate != 1
        assert publish_binding(writer, "right", candidate, expected_revision=3)
        writer.rollback()
        assert read_binding(reader, "right") == (1, 3)

def test_content_identity_distinguishes_edits_and_renames_without_checkout_paths():
    original = {"pkg/app.py": "01" * 32, "test_app.py": "02" * 32}
    clean = content_identity(original)
    assert clean == content_identity(dict(reversed(list(original.items()))))
    assert clean != content_identity(original | {"pkg/app.py": "03" * 32})
    assert clean != content_identity(original | {"untracked.py": "01" * 32})
    assert clean != content_identity({"pkg/other.py": "01" * 32, "test_app.py": "02" * 32})
    assert clean != content_identity({"pkg/app.py": "01" * 32})
    for invalid in ("/checkout/pkg/app.py", "../app.py", "pkg//app.py"):
        with pytest.raises(ValueError, match="Invalid graph manifest path"):
            content_identity({invalid: "01" * 32})
