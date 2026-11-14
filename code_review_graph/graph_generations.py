"""Transactional checkout bindings for a shared graph database.

A generation identifies an immutable, resolved graph. Callers publish a
binding in the same transaction as its graph rows, after validating their
input. Readers resolve the binding in the same read transaction as the rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphNode, GraphStore


def content_identity(files: Mapping[str, str]) -> str:
    """Digest a manifest of canonical relative paths and parsed byte hashes.

    Input paths and hashes must describe the bytes consumed by the indexer.
    Commit IDs and checkout paths are deliberately outside this identity.
    """
    digest = hashlib.sha256()
    for path, file_digest in sorted(files.items()):
        relative = PurePosixPath(path)
        if (
            relative.is_absolute() or not relative.parts or ".." in relative.parts
            or relative.as_posix() != path or "\\" in path or "\0" in path
        ):
            raise ValueError(f"Invalid graph manifest path: {path!r}")
        raw_digest = bytes.fromhex(file_digest)
        if len(raw_digest) != digest.digest_size:
            raise ValueError(f"Invalid SHA-256 for graph manifest path: {path!r}")
        digest.update(os.fsencode(path) + b"\0")
        digest.update(raw_digest)
    return digest.hexdigest()


def indexed_file_manifest(repo_root: Path, store: GraphStore) -> dict[str, str]:
    """Return checkout-relative file hashes from the indexed bytes."""
    root = repo_root.resolve()
    return {
        (root / path).relative_to(root).as_posix(): digest
        for path, digest in store.get_file_hashes().items()
    }


def indexed_content_identity(repo_root: Path, store: GraphStore) -> str:
    return content_identity(indexed_file_manifest(repo_root, store))


def initialize_bindings(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        raise RuntimeError("Initialize the generation catalog before opening transactions")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS graph_generations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL,
            content_digest TEXT NOT NULL,
            profile_digest TEXT NOT NULL,
            UNIQUE (repository, content_digest, profile_digest)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS indexed_file_versions (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            digest TEXT NOT NULL,
            UNIQUE (path, digest)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS generation_files (
            generation INTEGER NOT NULL REFERENCES graph_generations(id) ON DELETE CASCADE,
            file_version INTEGER NOT NULL REFERENCES indexed_file_versions(id),
            PRIMARY KEY (generation, file_version)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS generation_symbols (
            generation INTEGER NOT NULL REFERENCES graph_generations(id) ON DELETE CASCADE,
            file_path TEXT NOT NULL,
            symbol TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (generation, file_path, symbol)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS generation_edges (
            generation INTEGER NOT NULL REFERENCES graph_generations(id) ON DELETE CASCADE,
            file_path TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            line INTEGER NOT NULL,
            PRIMARY KEY (generation, file_path, ordinal)
        ) WITHOUT ROWID
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checkout_bindings (
            checkout TEXT PRIMARY KEY,
            generation INTEGER NOT NULL REFERENCES graph_generations(id),
            revision INTEGER NOT NULL CHECK (revision > 0)
        )
    """)


def ensure_generation(
    conn: sqlite3.Connection, repository: str, content_digest: str, profile_digest: str,
) -> int:
    """Return the existing generation or reserve one in this write transaction."""
    if not conn.in_transaction:
        raise RuntimeError("Generation allocation requires a write transaction")
    key = (repository, content_digest, profile_digest)
    row = conn.execute(
        "SELECT id FROM graph_generations WHERE repository = ? "
        "AND content_digest = ? AND profile_digest = ?", key,
    ).fetchone()
    if row is not None:
        return int(row[0])
    cursor = conn.execute(
        "INSERT INTO graph_generations (repository, content_digest, profile_digest) "
        "VALUES (?, ?, ?)", key,
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def ensure_indexed_generation(
    conn: sqlite3.Connection, *, repository: str, profile_digest: str,
    repo_root: Path, store: GraphStore,
) -> int:
    """Persist the indexed manifest in the pending publication transaction."""
    files = indexed_file_manifest(repo_root, store)
    generation = ensure_generation(conn, repository, content_identity(files), profile_digest)
    versions = [(path, digest.lower()) for path, digest in files.items()]
    conn.executemany(
        "INSERT INTO indexed_file_versions (path, digest) VALUES (?, ?) "
        "ON CONFLICT(path, digest) DO NOTHING",
        versions,
    )
    conn.executemany(
        "INSERT INTO generation_files (generation, file_version) "
        "SELECT ?, id FROM indexed_file_versions WHERE path = ? AND digest = ? "
        "ON CONFLICT(generation, file_version) DO NOTHING",
        ((generation, path, digest) for path, digest in versions),
    )
    root = repo_root.resolve()
    prefix = root.as_posix().rstrip("/") + "/"
    for path in files:
        conn.executemany(
            "INSERT INTO generation_symbols VALUES (?, ?, ?, ?) "
            "ON CONFLICT(generation, file_path, symbol) DO NOTHING",
            (
                (generation, path, node.qualified_name.removeprefix(prefix),
                 json.dumps({
                     **asdict(node),
                     "file_path": path,
                     "qualified_name": node.qualified_name.removeprefix(prefix),
                     "parent_name": (node.parent_name.removeprefix(prefix)
                                     if node.parent_name else node.parent_name),
                     "qualified_parent": bool(node.parent_name
                                              and node.parent_name.startswith(prefix)),
                 }))
                for node in store.iter_nodes_by_file(str(root / path))
            ),
        )
        conn.executemany(
            "INSERT INTO generation_edges VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(generation, file_path, ordinal) DO NOTHING",
            (
                (generation, path, ordinal, edge.kind, edge.source_qualified.removeprefix(prefix),
                 edge.target_qualified.removeprefix(prefix), edge.line)
                for ordinal, edge in enumerate(store.iter_file_edges(str(root / path)))
            ),
        )
    return generation


def read_checkout_manifest(conn: sqlite3.Connection, checkout: str) -> dict[str, str]:
    """Read only the indexed paths bound to this checkout, in one statement."""
    return dict(conn.execute(
        "SELECT version.path, version.digest FROM checkout_bindings AS binding "
        "JOIN generation_files AS files ON files.generation = binding.generation "
        "JOIN indexed_file_versions AS version ON version.id = files.file_version "
        "WHERE binding.checkout = ?", (checkout,),
    ).fetchall())


def read_checkout_nodes(
    conn: sqlite3.Connection, checkout_root: Path, relative_path: str,
) -> list[GraphNode]:
    """Read a bound file's native nodes, projecting paths under its checkout."""
    from .graph import GraphNode

    root = checkout_root.resolve()
    rows = conn.execute(
        "SELECT symbols.payload FROM checkout_bindings AS binding "
        "JOIN generation_symbols AS symbols ON symbols.generation = binding.generation "
        "WHERE binding.checkout = ? AND symbols.file_path = ? ORDER BY symbols.symbol",
        (str(root), relative_path),
    )
    nodes = []
    prefix = root.as_posix().rstrip("/") + "/"
    for (payload,) in rows:
        fields = json.loads(payload)
        fields["file_path"] = (root / fields["file_path"]).as_posix()
        fields["qualified_name"] = prefix + fields["qualified_name"]
        if fields.pop("qualified_parent"):
            fields["parent_name"] = prefix + fields["parent_name"]
        nodes.append(GraphNode(**fields))
    return nodes


def read_checkout_edges(
    conn: sqlite3.Connection, checkout_root: Path, relative_path: str,
) -> list[tuple[str, str, str, int]]:
    """Read a file's edges from the generation bound to this checkout."""
    return [(kind, source, target, line) for kind, source, target, line in conn.execute(
        "SELECT edges.kind, edges.source, edges.target, edges.line "
        "FROM checkout_bindings AS binding "
        "JOIN generation_edges AS edges ON edges.generation = binding.generation "
        "WHERE binding.checkout = ? AND edges.file_path = ? ORDER BY edges.ordinal",
        (str(checkout_root.resolve()), relative_path),
    )]


def read_binding(conn: sqlite3.Connection, checkout: str) -> tuple[int, int] | None:
    """Return (generation, revision), or None before the first publication."""
    row = conn.execute(
        "SELECT generation, revision FROM checkout_bindings WHERE checkout = ?", (checkout,),
    ).fetchone()
    return (row[0], row[1]) if row is not None else None


def publish_binding(
    conn: sqlite3.Connection, checkout: str, generation: int, *, expected_revision: int,
) -> bool:
    """Publish once for the expected revision (zero means an unbound checkout).

    The revision increases even when reusing a previously served generation,
    so switching away and back cannot make stale work eligible to publish.
    """
    if not conn.in_transaction:
        raise RuntimeError("Checkout publication requires a write transaction")
    if expected_revision == 0:
        cursor = conn.execute(
            "INSERT INTO checkout_bindings VALUES (?, ?, 1) ON CONFLICT(checkout) DO NOTHING",
            (checkout, generation),
        )
    else:
        cursor = conn.execute(
            "UPDATE checkout_bindings SET generation = ?, revision = revision + 1 "
            "WHERE checkout = ? AND revision = ?",
            (generation, checkout, expected_revision),
        )
    return cursor.rowcount == 1


def publish_indexed_checkout(
    database: Path, repo_root: Path, store: GraphStore, *, profile_digest: str,
) -> int:
    """Publish an experimental read projection from a successful native write."""
    from .locking import WriteLock
    from .multi_worktree import _canonical_git_identity

    root, repository = _canonical_git_identity(repo_root)
    database = database.resolve()
    if database.parent == store.db_path.resolve().parent:
        raise ValueError("Shared projection requires a separate graph data directory")
    lock = WriteLock(database)
    if not lock.acquire(blocking=True):
        raise RuntimeError("Shared projection writer is contended")
    try:
        with closing(sqlite3.connect(database, isolation_level=None)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            initialize_bindings(conn)
            conn.execute("BEGIN IMMEDIATE")
            current = read_binding(conn, str(root))
            generation = ensure_indexed_generation(
                conn, repository=str(repository), profile_digest=profile_digest,
                repo_root=root, store=store,
            )
            if current is None or current[0] != generation:
                if not publish_binding(
                    conn, str(root), generation, expected_revision=current[1] if current else 0,
                ):
                    raise RuntimeError("Shared checkout binding changed during publication")
            conn.commit()
            return generation
    finally:
        lock.release()
