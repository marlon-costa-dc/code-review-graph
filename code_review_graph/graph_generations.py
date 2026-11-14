"""Transactional checkout bindings for a shared graph database.

A generation identifies an immutable, resolved graph. Callers publish a
binding in the same transaction as its graph rows, after validating their
input. Readers resolve the binding in the same read transaction as the rows.
"""

from __future__ import annotations

import sqlite3


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
