"""Transactional checkout bindings for a shared graph database.

A generation identifies an immutable, resolved graph. Callers publish a
binding in the same transaction as its graph rows, after validating their
input. Readers resolve the binding in the same read transaction as the rows.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .graph import GraphStore


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
    return generation


def read_checkout_manifest(conn: sqlite3.Connection, checkout: str) -> dict[str, str]:
    """Read only the indexed paths bound to this checkout, in one statement."""
    return dict(conn.execute(
        "SELECT version.path, version.digest FROM checkout_bindings AS binding "
        "JOIN generation_files AS files ON files.generation = binding.generation "
        "JOIN indexed_file_versions AS version ON version.id = files.file_version "
        "WHERE binding.checkout = ?", (checkout,),
    ).fetchall())


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
