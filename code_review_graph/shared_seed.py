"""Seed an empty shared checkout from a compatible published peer generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .build_state import BUILD_COMPLETE, BUILD_STATE_KEY
from .constants import normalize_file_path
from .errors import GraphStoreError
from .graph import GraphStore
from .incremental import (
    _CPP_IDENTITY_METADATA_KEY,
    _CSHARP_IDENTITY_METADATA_KEY,
    _NESTED_SCOPE_METADATA_KEY,
    _store_vcs_metadata,
    incremental_update,
)
from .multi_worktree import discover_worktrees
from .repository_identity import repository_scopes
from .shared_generations import git_ref, manifest_at
from .shared_sqlite import UnbuiltCheckoutError

_SEED_METADATA = (
    _CPP_IDENTITY_METADATA_KEY, _CSHARP_IDENTITY_METADATA_KEY, _NESTED_SCOPE_METADATA_KEY,
    "last_updated", "last_build_type", "postprocess_level",
)
SEED_PROFILE_KEY = "generation_seed_profile"


def seed_checkout(
    store: GraphStore, *, profile: str, manifest: str, files: dict[str, str], head: str,
    source: str = "auto", enrich: bool | None = None,
) -> dict[str, Any] | None:
    """Restore a complete peer graph and update only divergent file inputs."""
    root = store.checkout
    if root is None or store.has_nodes() or store.get_metadata("last_build_type"):
        if source != "auto":
            raise GraphStoreError("An explicit seed requires an empty shared checkout")
        return None
    if not head or not profile:
        if source != "auto":
            raise GraphStoreError("Seeding requires Git and a portable full indexing profile")
        return None

    if source == "auto":
        peers = [checkout.root for checkout in discover_worktrees(root) if checkout.root != root]
    else:
        peer = Path(source).expanduser()
        if not peer.is_absolute() or not peer.is_dir() or peer.resolve() == root:
            raise GraphStoreError("Select a different absolute checkout path as the shared seed")
        peer = peer.resolve()
        if set(repository_scopes(root)).isdisjoint(repository_scopes(peer)):
            raise GraphStoreError(f"Seed checkout {peer} does not share a Git identity with {root}")
        peers = [peer]

    seed: tuple[Path, str, str, dict[str, str]] | None = None
    for peer in peers:
        try:
            candidate = GraphStore(store.db_path, checkout=peer, read_only=True)
        except UnbuiltCheckoutError:
            continue
        with candidate:
            if (
                candidate.get_metadata(BUILD_STATE_KEY) != BUILD_COMPLETE
                or candidate.get_metadata("postprocess_level") != "full"
                or candidate.get_metadata(SEED_PROFILE_KEY) != profile
            ):
                continue
            base_head = candidate.get_metadata("git_head_sha")
            if not base_head:
                continue
            base_branch = candidate.get_metadata("git_branch") or ""
            ref = git_ref(base_branch, base_head)
            base_manifest = manifest_at(candidate._conn, peer, ref, base_head)
            if base_manifest is None:
                continue
            metadata = {key: value for key in _SEED_METADATA
                        if (value := candidate.get_metadata(key)) is not None}
            exact = base_manifest == manifest
            if seed is None or exact:
                seed = peer, ref, base_head, metadata
            if exact:
                break
    if seed is None:
        if source != "auto":
            raise GraphStoreError(f"Seed checkout {source} has no compatible complete generation")
        return None

    peer, ref, base_head, metadata = seed
    generation = store.seed_generation(peer, ref, base_head)
    for key, value in metadata.items():
        store.set_metadata(key, value)

    prefix = normalize_file_path(root).rstrip("/") + "/"
    previous = {path.removeprefix(prefix): digest
                for path, digest in store.get_file_hashes().items()}
    changed = sorted(path for path in previous.keys() | files.keys()
                     if previous.get(path) != files.get(path))
    result: dict[str, Any] = {
        "files_parsed": 0, "files_updated": 0, "changed_files": [], "errors": [],
    }
    if changed:
        result = incremental_update(root, store, changed_files=changed, enrich=enrich)
    else:
        result["seeded_generation"] = (generation, peer, ref)
    _store_vcs_metadata(root, store)
    return {**result, "seeded_from": str(peer), "seed_base_commit": base_head}
