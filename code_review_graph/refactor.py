"""Graph-powered refactoring operations.

Provides rename previews, dead code detection, refactoring suggestions,
and safe application of refactoring edits to source files. All file writes
go through a preview-then-apply workflow with expiry enforcement and path
traversal prevention.
"""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional, Union

from .flows import _has_framework_decorator, _matches_entry_name
from .graph import GraphStore, _sanitize_name

logger = logging.getLogger(__name__)

# Base class names that indicate a framework-managed class (ORM models,
# Pydantic schemas, settings).  Classes inheriting from these are invoked
# via metaclass/framework magic and should not be flagged as dead code.
_FRAMEWORK_BASE_CLASSES = frozenset({
    "Base", "DeclarativeBase", "Model", "BaseModel", "BaseSettings",
    "db.Model", "TableBase",
    # AWS CDK constructs -- instantiated by CDK app wiring, not explicit CALLS.
    "Stack", "NestedStack", "Construct", "Resource",
    # Protocol classes are interface definitions; their methods are contract,
    # not dead code.
    "Protocol", "typing.Protocol",
    # Test framework bases -- test helpers and TestCase subclasses are wired by runner.
    "TestCase", "unittest.TestCase", "AsyncTestCase", "IsolatedAsyncioTestCase",
    # Python stdlib handlers -- instantiated by the HTTP server framework.
    "BaseHTTPRequestHandler",
    # Tornado handlers -- subclassed and wired by the Tornado app.
    "RequestHandler", "tornado.web.RequestHandler",
    "WebSocketHandler", "tornado.websocket.WebSocketHandler",
})

# Class name suffixes that indicate CDK/IaC constructs.
# These are instantiated by framework wiring, not direct CALLS edges.
# Used as fallback when INHERITS edges to external base classes are absent.
_CDK_CLASS_SUFFIXES = ("Stack", "Construct", "Pipeline", "Resources", "Layer")

# Python stdlib/base classes that mark a class as a value namespace (enum).
_ENUM_BASE_CLASSES = frozenset({
    "Enum", "enum.Enum", "StrEnum", "enum.StrEnum",
    "IntEnum", "enum.IntEnum", "Flag", "enum.Flag",
})

# Patterns for mock/stub variables in test files that should not be flagged dead.
_MOCK_NAME_RE = re.compile(
    r"^(mock[A-Z_]|Mock[A-Z]|createMock[A-Z])|"  # mockDynamoClient, MockService, createMockX
    r"(Mock|Stub|Fake|Spy)$",                      # s3ClientMock, dbStub
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Thread-safe pending refactors storage
# ---------------------------------------------------------------------------

_refactor_lock = threading.Lock()
_pending_refactors: dict[str, dict] = {}
REFACTOR_EXPIRY_SECONDS = 600  # 10 minutes


def _cleanup_expired() -> int:
    """Remove expired refactors from the pending dict.  Returns count removed."""
    now = time.time()
    expired = [
        rid for rid, r in _pending_refactors.items()
        if now - r["created_at"] > REFACTOR_EXPIRY_SECONDS
    ]
    for rid in expired:
        del _pending_refactors[rid]
    return len(expired)


# ---------------------------------------------------------------------------
# 1. rename_preview
# ---------------------------------------------------------------------------


def rename_preview(
    store: GraphStore,
    old_name: str,
    new_name: str,
) -> Optional[dict[str, Any]]:
    """Build a rename edit list for *old_name* -> *new_name*.

    Finds the node via ``store.search_nodes(old_name)``, collects
    definition and reference sites, generates a unique ``refactor_id``,
    and stores the preview in the thread-safe ``_pending_refactors`` dict.

    Returns:
        A refactor preview dict, or ``None`` if the node is not found.
    """
    candidates = store.search_nodes(old_name, limit=10)
    # Pick the best match: prefer exact name match.
    node = None
    for c in candidates:
        if c.name == old_name:
            node = c
            break
    if node is None and candidates:
        node = candidates[0]
    if node is None:
        logger.warning("rename_preview: node %r not found", old_name)
        return None

    edits: list[dict[str, Any]] = []

    # --- Definition site ---
    edits.append({
        "file": node.file_path,
        "line": node.line_start,
        "old": old_name,
        "new": new_name,
        "confidence": "high",
    })

    # --- Call sites (CALLS edges targeting this node) ---
    call_edges = store.get_edges_by_target(node.qualified_name)
    for edge in call_edges:
        if edge.kind == "CALLS":
            edits.append({
                "file": edge.file_path,
                "line": edge.line,
                "old": old_name,
                "new": new_name,
                "confidence": "high",
            })

    # Also search by bare name for unqualified edges.
    bare_edges = store.search_edges_by_target_name(
        old_name,
        kind="CALLS",
        language=node.language or None,
    )
    seen = {(e["file"], e["line"]) for e in edits}
    for edge in bare_edges:
        key = (edge.file_path, edge.line)
        if key not in seen:
            edits.append({
                "file": edge.file_path,
                "line": edge.line,
                "old": old_name,
                "new": new_name,
                "confidence": "high",
            })
            seen.add(key)

    # --- Import sites (IMPORTS_FROM edges targeting this node) ---
    import_edges = store.get_edges_by_target(node.qualified_name)
    for edge in import_edges:
        if edge.kind == "IMPORTS_FROM":
            key = (edge.file_path, edge.line)
            if key not in seen:
                edits.append({
                    "file": edge.file_path,
                    "line": edge.line,
                    "old": old_name,
                    "new": new_name,
                    "confidence": "high",
                })
                seen.add(key)

    # --- Stats ---
    stats = {"high": 0, "medium": 0, "low": 0}
    for e in edits:
        stats[e["confidence"]] += 1

    refactor_id = uuid.uuid4().hex[:8]
    preview: dict[str, Any] = {
        "refactor_id": refactor_id,
        "type": "rename",
        "old_name": _sanitize_name(old_name),
        "new_name": _sanitize_name(new_name),
        "edits": edits,
        "stats": stats,
        "created_at": time.time(),
    }

    with _refactor_lock:
        _cleanup_expired()
        _pending_refactors[refactor_id] = preview

    logger.info(
        "rename_preview: created refactor %s (%s -> %s, %d edits)",
        refactor_id, old_name, new_name, len(edits),
    )
    return preview


# ---------------------------------------------------------------------------
# 2. find_dead_code
# ---------------------------------------------------------------------------


# Cache for argparse subcommand detection: file_path -> set of function names
# registered via ``subparser.set_defaults(func=...)``.
_ARGPARSE_SUBCOMMAND_CACHE: dict[str, frozenset[str]] = {}

# Pattern matching ``set_defaults(func=command_name)`` or
# ``set_defaults(func=some.attr.command_name)``.
_ARGPARSE_SUBCOMMAND_RE = re.compile(
    r"\.set_defaults\(\s*func\s*=\s*(?:[\w]+\.)*([\w]+)\s*[,)]",
    re.MULTILINE,
)


def _is_argparse_subcommand(node: Any) -> bool:
    """Return True if *node* is registered as an argparse subcommand handler.

    Detects ``subparser.set_defaults(func=handler_name)`` patterns so that
    CLI subcommands are not flagged as dead code even though they have no
    explicit CALLS edges.
    """
    file_path = node.file_path
    if not file_path or not Path(file_path).is_file():
        return False
    cached = _ARGPARSE_SUBCOMMAND_CACHE.get(file_path)
    if cached is None:
        try:
            text = Path(file_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            cached = frozenset()
        else:
            cached = frozenset(_ARGPARSE_SUBCOMMAND_RE.findall(text))
        _ARGPARSE_SUBCOMMAND_CACHE[file_path] = cached
    return node.name in cached


def _is_entry_point(node: Any) -> bool:
    """Check if a node looks like an entry point by name, decorator or CLI registration.

    Unlike ``flows.detect_entry_points()`` which treats ALL uncalled functions
    as entry points, this checks only for conventional name patterns,
    framework decorators, and CLI wiring patterns -- the indicators that a
    function is *intentionally* an entry point rather than simply unreferenced
    dead code.
    """
    if _has_framework_decorator(node):
        return True
    if _matches_entry_name(node):
        return True
    if _is_argparse_subcommand(node):
        return True
    return False


# Matches identifiers inside type annotations (e.g. "GoalCreate" in
# "body: GoalCreate", "Optional[UserResponse]", "list[Item]").
_TEST_FILE_RE = re.compile(
    r"([\\/]__tests__[\\/]|\.spec\.[jt]sx?$|\.test\.[jt]sx?$|[\\/]test_[^/\\]*\.py$"
    r"|[\\/][^/\\]*_tests?\.py$|[\\/]conftest\.py$"
    r"|[\\/]e2e[_-]?tests?[\\/]|[\\/]test[_-]utils?[\\/])"
)


def _is_test_file(file_path: str) -> bool:
    """Return True if *file_path* looks like a test file."""
    return bool(_TEST_FILE_RE.search(file_path))


_MIN_PKG_SEGMENT_LEN = 4  # ignore short dirs like "src", "lib", "app"


@functools.lru_cache(maxsize=4096)
def _path_segments(file_path: str) -> tuple[str, ...]:
    """Return directory segments long enough to serve as package-name anchors."""
    parts = file_path.replace("\\", "/").split("/")
    return tuple(
        p for p in parts[:-1]  # skip the filename itself
        if len(p) >= _MIN_PKG_SEGMENT_LEN and p not in ("home", "src", "lib", "app")
    )


_TYPE_IDENT_RE = re.compile(r"[A-Z][A-Za-z0-9_]*")

# Method calls that appear anywhere in a source file, including inside
# f-string interpolations that tree-sitter may not traverse as ordinary
# call_expression nodes.  Used as a fallback to avoid flagging methods
# that are clearly invoked in the same file.
_FILE_METHOD_CALL_RE = re.compile(r"\.\s*([A-Za-z_]\w*)\s*\(")


class _DeadCodeContext:
    """Shared context and precomputed lookups for dead-code analysis.

    This class encapsulates the data preparation that was previously inlined
    in ``find_dead_code``.  It now loads all relevant edges in a single batch
    and builds in-memory indexes so the per-node detector performs lookups
    instead of issuing O(N) SQLite round-trips.
    """

    # Edge kinds required by the dead-code detector.
    _EDGE_KINDS = ("CALLS", "TESTED_BY", "IMPORTS_FROM", "INHERITS", "REFERENCES")

    def __init__(self, store: GraphStore, root: Optional[Union[str, Path]]) -> None:
        self.store = store
        self.root = root
        self.conn = store._conn
        # Load all Function/Class/Test nodes once and derive name stats.
        all_nodes = self.store.get_nodes_by_kind(kinds=["Function", "Class", "Test"])
        self.type_ref_names = self._collect_type_referenced_names(all_nodes)
        self.name_counts = self._build_name_counts(all_nodes)
        # Class hierarchy and MRO.
        self.class_bases = self._build_class_bases()
        self.class_name_to_qns = self._build_class_name_to_qns()
        self.mro_resolver = _MROResolver(self.class_bases, self.class_name_to_qns)
        # Import graph for bare-name plausibility.
        self.importer_files = self._build_importer_files()
        self.plausibility = _CallerPlausibilityChecker(
            self.importer_files, self.name_counts
        )
        # Batch-load and index all relevant edges.
        self.edges_by_kind = self._load_edges_by_kind()
        self.incoming, self.outgoing = self._build_adjacency_indexes()
        # Fallback: method names invoked anywhere in a source file (catches
        # f-string interpolations and other edge cases the parser may miss).
        self.file_method_calls = self._build_file_method_calls(all_nodes)
        self.call_targets_reachable = self._build_reachable_call_targets()
        self.calls_targets = self._build_calls_targets()
        self.bare_calls, self.partial_calls = self._build_bare_and_partial_calls()
        self.bare_inherits = self._build_bare_inherits()
        self.bare_tested_by_sources = self._build_bare_tested_by_sources()
        # Parent-child index for namespace/dataclass detection.
        self.class_children = self._build_class_children(all_nodes)
        self.class_method_names = self._build_class_method_names(all_nodes)
        self.class_qn_to_node = self._build_class_qn_to_node(all_nodes)

    def _build_class_qn_to_node(self, nodes: list[Any]) -> dict[str, Any]:
        """Map class qualified_name -> class node for O(1) class lookups."""
        mapping: dict[str, Any] = {}
        for n in nodes:
            if n.kind == "Class":
                if n.parent_name:
                    qn = f"{n.file_path}::{n.parent_name}.{n.name}"
                else:
                    qn = f"{n.file_path}::{n.name}"
                mapping[qn] = n
        return mapping

    def _collect_type_referenced_names(self, nodes: list[Any]) -> set[str]:
        """Collect class names that appear in function params or return types."""
        names: set[str] = set()
        for n in nodes:
            if n.kind in ("Function", "Test"):
                for text in (n.params, n.return_type):
                    if text:
                        names.update(_TYPE_IDENT_RE.findall(text))
        return names

    def _build_name_counts(self, nodes: list[Any]) -> dict[str, int]:
        """Count non-test Function/Class nodes per bare name.

        Globally unique names make bare-name CALLS edges unambiguous.
        """
        name_counts: dict[str, int] = {}
        for n in nodes:
            if n.kind in ("Function", "Class") and not n.is_test:
                name_counts[n.name] = name_counts.get(n.name, 0) + 1
        return name_counts

    def _build_file_method_calls(self, nodes: list[Any]) -> dict[str, set[str]]:
        """Map file_path -> set of method names invoked anywhere in the file.

        This catches method calls inside f-string interpolations and other
        contexts that tree-sitter may not expose as ordinary call_expression
        nodes, preventing false-positive dead-code flags.
        """
        file_method_calls: dict[str, set[str]] = {}
        files_seen: set[str] = set()
        for n in nodes:
            fp = n.file_path
            if not fp or fp in files_seen:
                continue
            files_seen.add(fp)
            path = Path(fp)
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            file_method_calls[fp] = set(_FILE_METHOD_CALL_RE.findall(text))
        return file_method_calls

    def _build_class_bases(self) -> dict[str, list[str]]:
        """Build class hierarchy: class_qualified_name -> [bare_base_names]."""
        class_bases: dict[str, list[str]] = {}
        for row in self.conn.execute(
            "SELECT source_qualified, target_qualified FROM edges WHERE kind = 'INHERITS'"
        ).fetchall():
            base = row[1].rsplit("::", 1)[-1] if "::" in row[1] else row[1]
            class_bases.setdefault(row[0], []).append(base)
        return class_bases

    def _build_class_name_to_qns(self) -> dict[str, set[str]]:
        """Map bare class name -> set of fully-qualified class names."""
        class_name_to_qns: dict[str, set[str]] = {}
        for row in self.conn.execute(
            "SELECT name, qualified_name FROM nodes WHERE kind = 'Class'"
        ).fetchall():
            class_name_to_qns.setdefault(row[0], set()).add(row[1])
        return class_name_to_qns

    def _build_importer_files(self) -> dict[str, set[str]]:
        """Build import graph: file_path -> set of import targets.

        Used to filter bare-name caller matches to plausible callers.
        """
        importer_files: dict[str, set[str]] = {}
        for row in self.conn.execute(
            "SELECT file_path, target_qualified FROM edges WHERE kind = 'IMPORTS_FROM'"
        ).fetchall():
            importer_files.setdefault(row[0], set()).add(row[1])
        return importer_files

    def _load_edges_by_kind(self) -> dict[str, list[Any]]:
        """Load all edges of the kinds used by dead-code detection in one query."""
        placeholders = ",".join("?" for _ in self._EDGE_KINDS)
        rows = self.conn.execute(
            f"SELECT * FROM edges WHERE kind IN ({placeholders})",  # nosec B608
            list(self._EDGE_KINDS),
        ).fetchall()
        edges_by_kind: dict[str, list[Any]] = {k: [] for k in self._EDGE_KINDS}
        for row in rows:
            edge = self.store._row_to_edge(row)
            edges_by_kind[edge.kind].append(edge)
        return edges_by_kind

    def _build_adjacency_indexes(
        self,
    ) -> tuple[dict[str, dict[str, list[Any]]], dict[str, dict[str, list[Any]]]]:
        """Build incoming/outgoing adjacency dicts per edge kind."""
        incoming: dict[str, dict[str, list[Any]]] = {
            k: {} for k in self._EDGE_KINDS
        }
        outgoing: dict[str, dict[str, list[Any]]] = {
            k: {} for k in self._EDGE_KINDS
        }
        for kind, edges in self.edges_by_kind.items():
            in_kind = incoming[kind]
            out_kind = outgoing[kind]
            for edge in edges:
                in_kind.setdefault(edge.target_qualified, []).append(edge)
                out_kind.setdefault(edge.source_qualified, []).append(edge)
        return incoming, outgoing

    def _build_reachable_call_targets(self) -> set[str]:
        """Return the set of CALLS targets with reachable=True (default True)."""
        reachable: set[str] = set()
        for edge in self.edges_by_kind.get("CALLS", ()):
            if edge.extra.get("reachable", True):
                reachable.add(edge.target_qualified)
        return reachable

    def _build_calls_targets(self) -> set[str]:
        """Return the set of all CALLS target qualified names."""
        return {e.target_qualified for e in self.edges_by_kind.get("CALLS", ())}

    def _build_bare_and_partial_calls(self) -> tuple[dict[str, list[Any]], dict[str, list[Any]]]:
        """Index CALLS edges by bare target name.

        ``bare_calls`` maps unqualified target names (no ``::``).
        ``partial_calls`` maps the trailing name segment of qualified targets
        so that ``Class::method`` and ``file.py::Class.method`` can be found by
        searching for ``method``.
        """
        bare_calls: dict[str, list[Any]] = {}
        partial_calls: dict[str, list[Any]] = {}
        for edge in self.edges_by_kind.get("CALLS", ()):
            target = edge.target_qualified
            if "::" not in target:
                bare_calls.setdefault(target, []).append(edge)
            else:
                name = target.rsplit("::", 1)[-1]
                partial_calls.setdefault(name, []).append(edge)
        return bare_calls, partial_calls

    def _build_bare_inherits(self) -> dict[str, list[Any]]:
        """Index INHERITS edges whose target is an unqualified class name."""
        bare_inherits: dict[str, list[Any]] = {}
        for edge in self.edges_by_kind.get("INHERITS", ()):
            if "::" not in edge.target_qualified:
                bare_inherits.setdefault(edge.target_qualified, []).append(edge)
        return bare_inherits

    def _build_bare_tested_by_sources(self) -> dict[str, list[Any]]:
        """Index TESTED_BY edges whose source is an unqualified function name."""
        bare_tested_by: dict[str, list[Any]] = {}
        for edge in self.edges_by_kind.get("TESTED_BY", ()):
            if "::" not in edge.source_qualified:
                bare_tested_by.setdefault(edge.source_qualified, []).append(edge)
        return bare_tested_by

    def _build_class_children(
        self, nodes: list[Any]
    ) -> dict[str, list[Any]]:
        """Map class qualified_name -> immediate child nodes (Class/Function)."""
        children: dict[str, list[Any]] = {}
        for n in nodes:
            if n.parent_name and n.kind in ("Class", "Function", "Test"):
                # Reconstruct parent qualified name from child info.
                if "::" in n.qualified_name:
                    parent_qn = n.qualified_name.rsplit(".", 1)[0]
                else:
                    parent_qn = f"{n.file_path}::{n.parent_name}"
                children.setdefault(parent_qn, []).append(n)
        return children

    def _build_class_method_names(
        self, nodes: list[Any]
    ) -> dict[str, set[str]]:
        """Map class qualified_name -> set of method names defined in the class."""
        method_names: dict[str, set[str]] = {}
        for n in nodes:
            if n.kind == "Function" and n.parent_name:
                if "::" in n.qualified_name:
                    parent_qn = n.qualified_name.rsplit(".", 1)[0]
                else:
                    parent_qn = f"{n.file_path}::{n.parent_name}"
                method_names.setdefault(parent_qn, set()).add(n.name)
        return method_names

    def relative_path(self, file_path: str) -> str:
        if self.root:
            try:
                return str(Path(file_path).relative_to(self.root))
            except ValueError:
                return file_path
        return file_path


class _MROResolver:
    """Resolve inheritance-connected components for polymorphic dispatch.

    A method reachable through the inheritance chain should not be flagged dead.
    Bare base names are resolved to class nodes so cross-file inheritance links
    up. The graph is undirected (child <-> base) so dispatch works both ways:
    a call to a base method keeps every override alive, and a call to an
    override keeps the inherited base method reachable. See OO/MRO false
    positives.
    """

    def __init__(
        self,
        class_bases: dict[str, list[str]],
        class_name_to_qns: dict[str, set[str]],
    ) -> None:
        self._mro_adj = self._build_mro_adj(class_bases, class_name_to_qns)
        self._mro_component_cache: dict[str, frozenset[str]] = {}

    @staticmethod
    def _build_mro_adj(
        class_bases: dict[str, list[str]],
        class_name_to_qns: dict[str, set[str]],
    ) -> dict[str, set[str]]:
        mro_adj: dict[str, set[str]] = {}
        for child_qn, base_list in class_bases.items():
            for base in base_list:
                for base_qn in class_name_to_qns.get(base, ()):
                    if base_qn != child_qn:
                        mro_adj.setdefault(child_qn, set()).add(base_qn)
                        mro_adj.setdefault(base_qn, set()).add(child_qn)
        return mro_adj

    def component(self, class_qn: str) -> frozenset[str]:
        """Return every class qn reachable from *class_qn* via inheritance."""
        cached = self._mro_component_cache.get(class_qn)
        if cached is not None:
            return cached
        seen: set[str] = set()
        stack = [class_qn]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(self._mro_adj.get(cur, ()))
        frozen = frozenset(seen)
        for member in seen:
            self._mro_component_cache[member] = frozen
        return frozen


class _CallerPlausibilityChecker:
    """Decide whether a bare-name CALLS/TESTED_BY edge can reach a node."""

    def __init__(
        self,
        importer_files: dict[str, set[str]],
        name_counts: dict[str, int],
    ) -> None:
        self.importer_files = importer_files
        self.name_counts = name_counts

    def is_plausible(
        self, edge_file: str, node_file: str, node_name: str = ""
    ) -> bool:
        """A bare-name edge is plausible if it comes from the same file,
        from a file that has an IMPORTS_FROM edge whose target matches
        the node's file path, or the name is globally unique (no ambiguity).
        """
        if edge_file == node_file:
            return True
        # Unique names (only one definition) have no ambiguity -- accept all callers.
        if node_name and self.name_counts.get(node_name, 0) == 1:
            return True
        for imp_target in self.importer_files.get(edge_file, ()):
            # Strip "::name" suffix — workspace-resolved imports may include it
            imp_path = imp_target.split("::")[0] if "::" in imp_target else imp_target
            # __init__.py represents its parent package directory
            if imp_path.endswith("/__init__.py"):
                imp_dir = imp_path[:-12]  # strip "/__init__.py"
                if node_file.startswith(imp_dir + "/"):
                    return True
            if imp_path.startswith(node_file) or node_file.startswith(imp_path + "/"):
                return True
            # 2-hop: edge_file imports X, X re-exports from node_file (barrel files)
            for imp2 in self.importer_files.get(imp_target, ()):
                imp2_path = imp2.split("::")[0] if "::" in imp2 else imp2
                if imp2_path.endswith("/__init__.py"):
                    imp2_dir = imp2_path[:-12]
                    if node_file.startswith(imp2_dir + "/"):
                        return True
                if imp2_path.startswith(node_file) or node_file.startswith(imp2_path + "/"):
                    return True
            # Package-alias heuristic: monorepo imports like "@scope/pkg-name"
            # contain the directory name of the target package.  Check if the
            # import target string contains a significant directory segment from
            # the node's file path (e.g. "lambda-common" in both the import
            # "@cova-utils/lambda-common" and the path "libraries/lambda-common/...").
            if not imp_target.startswith("/"):
                # imp_target is a package specifier, not a file path
                for seg in _path_segments(node_file):
                    if seg in imp_target:
                        return True
        return False


class _NodeFilter:
    """Apply exclusion heuristics to candidate nodes."""

    def __init__(self, context: _DeadCodeContext) -> None:
        self.context = context

    def should_skip(self, node: Any) -> bool:
        """Return True if *node* should be excluded from dead-code analysis."""
        # Skip test nodes and anything defined in test files.
        if node.is_test or _is_test_file(node.file_path):
            return True

        # Skip ambient type declarations (.d.ts) — they describe external APIs.
        if node.file_path.endswith(".d.ts"):
            return True

        # Skip dunder methods -- invoked by runtime, never have explicit callers.
        if node.name.startswith("__") and node.name.endswith("__"):
            return True

        # Skip JS/TS/Java constructors -- invoked via `new ClassName()`, which
        # creates a CALLS edge to the class, not to `constructor`.
        if node.name == "constructor" and node.parent_name:
            return True

        # Skip entry points (by name pattern or decorator, not just "uncalled").
        if _is_entry_point(node):
            return True

        # Skip classes referenced in type annotations (Pydantic schemas, etc.).
        if node.kind == "Class" and node.name in self.context.type_ref_names:
            return True

        # Skip Angular/NestJS decorated classes -- they are framework-managed
        # and instantiated by the DI container, not direct CALLS edges.
        if node.kind == "Class" and _has_framework_decorator(node):
            return True

        # Skip classes (and their methods) inheriting from known framework bases.
        is_framework_class = self._is_framework_class(node)
        if node.kind == "Class":
            if is_framework_class:
                return True
            # Fallback: CDK class name suffixes (no INHERITS edge for external bases)
            if any(node.name.endswith(s) for s in _CDK_CLASS_SUFFIXES):
                return True
        if node.kind == "Function" and is_framework_class:
            return True
        # Also skip methods whose parent class name matches CDK suffixes
        # (fallback for external base classes without INHERITS edges).
        if (
            node.kind == "Function"
            and node.parent_name
            and any(node.parent_name.endswith(s) for s in _CDK_CLASS_SUFFIXES)
        ):
            return True

        # Skip enum classes and their members -- values are referenced by name
        # and static analysis rarely captures every enum access.
        if self._is_enum_class(node):
            return True

        # Skip namespace classes that only contain nested types/constants.
        if node.kind == "Class" and self._is_namespace_class(node):
            return True

        # Skip methods/functions registered as event/dispatch handlers.
        # Names like `_handle_*`, `_format_*`, `_process_*` are conventionally
        # wired by reflection/dispatch tables, not direct CALLS edges.
        if node.kind == "Function" and self._is_dispatch_handler(node):
            return True

        # Skip methods of @dataclass classes -- they are typically helpers or
        # properties on data containers and are kept alive by the class usage.
        if node.kind == "Function" and self._is_dataclass_method(node):
            return True

        # Skip decorated functions/classes that are invoked implicitly rather
        # than via explicit CALLS edges.
        decorators = node.extra.get("decorators", ())
        if isinstance(decorators, (list, tuple)) and decorators:
            if node.kind in ("Function", "Test"):
                # @property -- invoked via attribute access
                # @abstractmethod -- polymorphic dispatch, never called directly
                # @classmethod/@staticmethod -- called via Class.method()
                if any(
                    d in ("property", "abstractmethod", "classmethod", "staticmethod")
                    or d.endswith(".abstractmethod")
                    # Angular @HostListener -- method called by framework event system
                    or d.startswith("HostListener")
                    for d in decorators
                ):
                    return True
            if node.kind == "Class":
                # @dataclass classes are instantiated as types, not via CALLS
                if any("dataclass" in d for d in decorators):
                    return True

        # Skip methods that override an @abstractmethod in a base class --
        # they are called polymorphically via the base class reference.
        if self._is_abstract_override(node):
            return True

        return False

    def _is_enum_class(self, node: Any) -> bool:
        """Return True if the node is an enum class (or member of one).

        Detects Python Enum/StrEnum/IntEnum/Flag via inheritance edges and
        Swift enums via extra["swift_kind"].
        """
        class_qn = self._class_qn_for(node)
        if not class_qn:
            return False
        class_node = self.context.class_qn_to_node.get(class_qn)
        if class_node is None:
            return False
        # Swift / language-specific enum keyword.
        if class_node.extra.get("swift_kind") == "enum":
            return True
        # Python enum via inheritance chain.
        seen: set[str] = set()
        stack = [class_qn]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for base in self.context.class_bases.get(cur, ()):
                if base in _ENUM_BASE_CLASSES:
                    return True
                # Resolve bare base name to qualified class names.
                for base_qn in self.context.class_name_to_qns.get(base, ()):
                    stack.append(base_qn)
        return False

    def _is_namespace_class(self, node: Any) -> bool:
        """Return True if the class is a pure namespace (nested types/constants only).

        Namespace classes contain nested classes or type aliases but no
        callable methods.  They are referenced as containers, not instantiated.
        """
        if node.kind != "Class":
            return False
        class_qn = self._class_qn_for(node)
        children = self.context.class_children.get(class_qn, [])
        if not children:
            return False
        # Allow nested classes/types; reject callable members.
        for child in children:
            if child.kind in ("Class", "Type"):
                continue
            if child.kind == "Function" and child.name == "__init__":
                # dataclass-like generated init still means data, not namespace
                continue
            return False
        return True

    def _is_dispatch_handler(self, node: Any) -> bool:
        """Return True if the function name follows reflection/dispatch conventions.

        Names like ``_handle_*``, ``_format_*``, ``_process_*`` are frequently
        wired by dispatch tables, registries, or reflection and lack explicit
        CALLS edges.  This is intentionally conservative: only well-known
        prefixes are matched.
        """
        if node.kind != "Function":
            return False
        name = node.name
        # Require a leading underscore so we only flag conventionally-private
        # helper methods that are wired by reflection/dispatch tables.  Public
        # names like ``format_date`` are regular utilities and must remain
        # eligible for dead-code detection.
        prefixes = (
            "_handle_", "_format_", "_process_", "_parse_", "_serialize_",
            "_validate_", "_dispatch_", "_route_", "_emit_", "_on_",
        )
        if any(name.startswith(p) for p in prefixes):
            return True
        # Common bare handler names used by plugin/dispatch systems.
        bare = {"_handle", "_format", "_process", "_dispatch", "_route", "_emit"}
        return name in bare

    def _is_dataclass_method(self, node: Any) -> bool:
        """Return True if the method belongs to a @dataclass-decorated class."""
        if node.kind != "Function" or not node.parent_name:
            return False
        class_qn = node.qualified_name.rsplit(".", 1)[0]
        class_node = self.context.class_qn_to_node.get(class_qn)
        if class_node is None:
            return False
        decorators = class_node.extra.get("decorators", ())
        if not isinstance(decorators, (list, tuple)):
            return False
        return any("dataclass" in d for d in decorators)

    def _class_qn_for(self, node: Any) -> Optional[str]:
        """Return the qualified name of the class a node belongs to."""
        if node.kind == "Class":
            if node.parent_name:
                return f"{node.file_path}::{node.parent_name}.{node.name}"
            return f"{node.file_path}::{node.name}"
        if node.kind == "Function" and node.parent_name:
            return node.qualified_name.rsplit(".", 1)[0]
        return None

    def _is_framework_class(self, node: Any) -> bool:
        """Return True if the node (or its parent class) inherits a known framework base."""
        _check_qn = node.qualified_name if node.kind == "Class" else (
            node.qualified_name.rsplit(".", 1)[0] if node.parent_name else None
        )
        if _check_qn:
            outgoing = self.context.store.get_edges_by_source(_check_qn)
            base_names = {
                e.target_qualified.rsplit("::", 1)[-1]
                for e in outgoing if e.kind == "INHERITS"
            }
            if base_names & _FRAMEWORK_BASE_CLASSES:
                return True
        return False

    def _is_abstract_override(self, node: Any) -> bool:
        """Return True if *node* overrides an @abstractmethod in a base class."""
        if node.kind != "Function" or not node.parent_name:
            return False
        parent_qn = node.qualified_name.rsplit(".", 1)[0]
        parent_edges = self.context.store.get_edges_by_source(parent_qn)
        base_class_names = [
            e.target_qualified for e in parent_edges if e.kind == "INHERITS"
        ]
        for base_name in base_class_names:
            # Try fully-qualified base first, then bare name match
            base_method_qn = f"{base_name}.{node.name}"
            base_nodes = self.context.store.get_node(base_method_qn)
            if base_nodes is None:
                # Base class may be bare name -- search in same file
                base_method_qn2 = (
                    node.file_path + "::" + base_name + "." + node.name
                )
                base_nodes = self.context.store.get_node(base_method_qn2)
            if base_nodes is not None:
                base_decos = base_nodes.extra.get("decorators", ())
                if isinstance(base_decos, (list, tuple)) and any(
                    "abstractmethod" in d for d in base_decos
                ):
                    return True
        return False


class _DeadCodeDetector:
    """Orchestrate dead-code detection using shared context and filters."""

    def __init__(self, context: _DeadCodeContext) -> None:
        self.context = context
        self.node_filter = _NodeFilter(context)

    def find(self, candidates: list[Any]) -> list[dict[str, Any]]:
        """Return dead-code entries for *candidates*."""
        dead: list[dict[str, Any]] = []
        for node in candidates:
            if self.node_filter.should_skip(node):
                continue
            if self._has_references(node):
                continue
            if self._has_subclasses(node):
                continue
            if self._has_callers(node):
                continue
            if self._has_test_refs(node):
                continue
            if self._has_importers(node):
                continue
            if self._has_member_calls(node):
                continue
            if self._is_alive_via_mro(node):
                continue
            dead.append(self._to_dead_entry(node))
        return dead

    def _qualified_and_class_incoming(
        self, node: Any, kind: str
    ) -> list[Any]:
        """Return incoming *kind* edges for *node* plus class-qualified variants."""
        incoming = self.context.incoming[kind].get(node.qualified_name, [])
        if node.parent_name:
            class_qn = f"{node.parent_name}::{node.name}"
            class_incoming = self.context.incoming[kind].get(class_qn)
            if class_incoming:
                incoming = incoming + class_incoming
        return incoming

    def _has_references(self, node: Any) -> bool:
        """Return True if *node* is referenced as a value (maps, arrays, etc.)."""
        return bool(self._qualified_and_class_incoming(node, "REFERENCES"))

    def _has_subclasses(self, node: Any) -> bool:
        """Return True if *node* is a class with subclasses."""
        if node.kind != "Class":
            return False
        if self.context.incoming["INHERITS"].get(node.qualified_name):
            return True
        return bool(self.context.bare_inherits.get(node.name))

    def _collect_call_edges(self, node: Any) -> list[Any]:
        """Collect all CALLS edges targeting *node* (qualified, class-qualified,
        bare-name and partially-qualified), filtering bare-name edges by
        plausibility.
        """
        incoming = self._qualified_and_class_incoming(node, "CALLS")
        # Also check bare-name and partially-qualified edges.
        # CALLS targets may be bare ("funcName"), class-qualified
        # ("Class::method"), or workspace-qualified ("pkg/dir::funcName").
        if not incoming:
            bare = self.context.bare_calls.get(node.name, [])
            partial = self.context.partial_calls.get(node.name, [])
            all_bare = bare + partial
            all_bare = [
                e for e in all_bare
                if self.context.plausibility.is_plausible(
                    e.file_path, node.file_path, node.name
                )
            ]
            incoming = incoming + all_bare
        return incoming

    def _has_callers(self, node: Any) -> bool:
        """Return True if *node* has reachable CALLS edges targeting it."""
        # Fast path: precomputed set of reachable CALLS targets.
        if node.qualified_name in self.context.call_targets_reachable:
            return True
        if node.parent_name:
            class_qn = f"{node.parent_name}::{node.name}"
            if class_qn in self.context.call_targets_reachable:
                return True
        # Fall back to collecting edges (handles bare-name and partial targets).
        incoming = self._collect_call_edges(node)
        if any(
            e.kind == "CALLS" and e.extra.get("reachable", True)
            for e in incoming
        ):
            return True
        # Fallback: method calls inside f-string interpolations (and other
        # parser edge cases) may not produce CALLS edges.  If the same file
        # contains a `.method_name(` invocation, treat the method as reachable.
        if node.kind == "Function" and node.parent_name and node.file_path:
            called = self.context.file_method_calls.get(node.file_path, set())
            if node.name in called:
                return True
        return False

    def _has_test_refs(self, node: Any) -> bool:
        """Return True if *node* is referenced by tests.

        TESTED_BY edges are stored as source=production, target=test by the
        parser, so a tested production node is the *source* of its TESTED_BY
        edge -- look outgoing, not incoming. See: #515
        """
        outgoing_tb = self.context.outgoing["TESTED_BY"].get(
            node.qualified_name, []
        )[:]
        if not outgoing_tb and node.parent_name:
            # Class-qualified source (e.g. "ClassName::method") which lacks the
            # file-path prefix used in node.qualified_name.
            class_qn = f"{node.parent_name}::{node.name}"
            class_outgoing = self.context.outgoing["TESTED_BY"].get(class_qn)
            if class_outgoing:
                outgoing_tb = outgoing_tb + class_outgoing
        if not outgoing_tb:
            # Bare-name source fallback: unresolved TESTED_BY edges may store the
            # production function by its plain name (e.g. "authenticate").
            bare_rows = self.context.bare_tested_by_sources.get(node.name, [])
            outgoing_tb += [
                e for e in bare_rows
                if self.context.plausibility.is_plausible(
                    e.file_path, node.file_path, node.name
                )
            ]
        return bool(outgoing_tb)

    def _has_importers(self, node: Any) -> bool:
        """Return True if *node* is imported by another file."""
        return bool(self.context.incoming["IMPORTS_FROM"].get(node.qualified_name))

    def _has_member_calls(self, node: Any) -> bool:
        """For classes with no direct references, check if any member has callers."""
        if node.kind != "Class":
            return False
        member_prefix = node.qualified_name + "."
        bare_prefix = node.name + "."
        return any(
            member_prefix in t or bare_prefix in t
            for t in self.context.calls_targets
        )

    def _is_alive_via_mro(self, node: Any) -> bool:
        """Return True if a same-named method in the MRO component has callers."""
        if node.kind != "Function" or not node.parent_name or self._has_callers(node):
            return False
        method_suffix = "." + node.name
        if not node.qualified_name.endswith(method_suffix):
            return False
        class_qn = node.qualified_name[: -len(method_suffix)]
        # A method is alive if ANY class in the same MRO-connected
        # component defines a same-named method that has a caller.
        # A call to any relative dispatches to this method at
        # runtime, so overrides and inherited bases stay reachable.
        for rel_qn in self.context.mro_resolver.component(class_qn):
            if rel_qn == class_qn:
                continue
            sibling_qn = rel_qn + method_suffix
            if sibling_qn in self.context.call_targets_reachable:
                return True
            rel_name = rel_qn.rsplit("::", 1)[-1] if "::" in rel_qn else rel_qn
            if f"{rel_name}::{node.name}" in self.context.call_targets_reachable:
                return True
        return False

    def _to_dead_entry(self, node: Any) -> dict[str, Any]:
        return {
            "name": _sanitize_name(node.name),
            "qualified_name": _sanitize_name(node.qualified_name),
            "kind": node.kind,
            "file": node.file_path,
            "file_path": node.file_path,
            "relative_path": self.context.relative_path(node.file_path),
            "line": node.line_start,
            "language": node.language,
        }


def find_dead_code(
    store: GraphStore,
    kind: Optional[str] = None,
    file_pattern: Optional[str] = None,
    root: Optional[Union[str, Path]] = None,
) -> list[dict[str, Any]]:
    """Find functions/classes with no callers, no test refs, no importers, and no references.

    Entry points (functions matching framework decorators or conventional name
    patterns like ``main``, ``test_*``, ``handle_*``) are excluded.

    .. note::

        **Caveats — dynamic dispatch patterns.**  Static analysis cannot track
        all runtime-determined call patterns.  Functions registered via fully
        dynamic keys (``map[computedKey()] = fn``), ``Reflect.apply``, or
        runtime ``require()`` may still appear as dead code.  Treat results as
        hints, especially for TypeScript projects that use map-based dispatch,
        plugin registries, or dynamic requires.

    Args:
        store: The GraphStore instance.
        kind: Optional filter (e.g. ``"Function"`` or ``"Class"``).
        file_pattern: Optional file-path substring filter.
        root: Optional repo root path for computing ``relative_path``.

    Returns:
        List of dead-code dicts with name, qualified_name, kind, file_path,
        relative_path, line, and language fields.
    """
    candidates = store.get_nodes_by_kind(
        kinds=[kind] if kind else ["Function", "Class"],
        file_pattern=file_pattern,
    )
    context = _DeadCodeContext(store, root)
    detector = _DeadCodeDetector(context)
    dead = detector.find(candidates)
    logger.info("find_dead_code: found %d dead symbols", len(dead))
    return dead


# ---------------------------------------------------------------------------
# 3. suggest_refactorings
# ---------------------------------------------------------------------------


def suggest_refactorings(store: GraphStore) -> list[dict[str, Any]]:
    """Produce community-driven refactoring suggestions.

    Currently two categories:
    - **move**: Functions in Community A only called by Community B.
    - **remove**: Dead code (no callers, tests, or importers and not entry points).

    Returns:
        List of suggestion dicts with type, description, symbols, rationale.
    """
    suggestions: list[dict[str, Any]] = []

    # --- Dead code suggestions ---
    dead = find_dead_code(store)
    for d in dead:
        suggestions.append({
            "type": "remove",
            "description": f"Remove unused {d['kind'].lower()} '{d['name']}'",
            "symbols": [d["qualified_name"]],
            "rationale": "No callers, no test references, no importers, not an entry point.",
        })

    # --- Cross-community move suggestions ---
    # Only attempt if communities table exists and has data.
    community_rows = store.get_communities_list()

    if community_rows:
        # Build node -> community_id mapping.
        node_community: dict[str, int] = {}
        for crow in community_rows:
            cid = crow["id"]
            member_qns = store.get_community_member_qns(cid)
            for qn in member_qns:
                node_community[qn] = cid

        community_names: dict[int, str] = {
            r["id"]: r["name"] for r in community_rows
        }

        # Check functions called only by members of a different community.
        all_funcs = [
            node for node in store.get_nodes_by_kind(["Function"])
            if not node.extra.get("verilog_kind")
        ]

        for fnode in all_funcs:
            f_community = node_community.get(fnode.qualified_name)
            if f_community is None:
                continue

            incoming_calls = [
                e for e in store.get_edges_by_target(fnode.qualified_name)
                if e.kind == "CALLS"
            ]
            if not incoming_calls:
                continue

            caller_communities = set()
            for edge in incoming_calls:
                c_community = node_community.get(edge.source_qualified)
                if c_community is not None:
                    caller_communities.add(c_community)

            # If ALL callers are from a single *different* community, suggest move.
            if len(caller_communities) == 1:
                target_community = next(iter(caller_communities))
                if target_community != f_community:
                    src_name = community_names.get(f_community, f"community-{f_community}")
                    tgt_name = community_names.get(
                        target_community, f"community-{target_community}"
                    )
                    suggestions.append({
                        "type": "move",
                        "description": (
                            f"Move '{_sanitize_name(fnode.name)}' from "
                            f"'{src_name}' to '{tgt_name}'"
                        ),
                        "symbols": [_sanitize_name(fnode.qualified_name)],
                        "rationale": (
                            f"Function is in community '{src_name}' but only "
                            f"called by members of community '{tgt_name}'."
                        ),
                    })

    logger.info("suggest_refactorings: produced %d suggestions", len(suggestions))
    return suggestions


# ---------------------------------------------------------------------------
# 4. apply_refactor
# ---------------------------------------------------------------------------


def apply_refactor(
    refactor_id: str,
    repo_root: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a previously previewed refactoring to source files.

    Validates the refactor_id, checks expiry, ensures all edit paths are
    within the repo root, then performs exact string replacements on the
    target files.

    Args:
        refactor_id: ID from a prior ``rename_preview`` call.
        repo_root: Validated repository root path.
        dry_run: If True, compute the would-be changes and return a
            unified-diff representation per affected file, but do NOT
            write anything to disk. The ``refactor_id`` is preserved so
            the same preview can be committed afterwards via a second
            call without ``dry_run``. See: #176

    Returns:
        Status dict with applied count and modified files. When
        ``dry_run=True`` the dict additionally contains:

        - ``dry_run``: ``True``
        - ``would_modify``: list of file paths that would be changed
        - ``diffs``: map of file path → unified diff string showing the
          proposed change
    """
    repo_root = repo_root.resolve()

    with _refactor_lock:
        _cleanup_expired()
        preview = _pending_refactors.get(refactor_id)

    if preview is None:
        logger.warning("apply_refactor: unknown or expired refactor_id %s", refactor_id)
        return {"status": "error", "error": f"Refactor '{refactor_id}' not found or expired."}

    # Check expiry explicitly.
    age = time.time() - preview["created_at"]
    if age > REFACTOR_EXPIRY_SECONDS:
        with _refactor_lock:
            _pending_refactors.pop(refactor_id, None)
        logger.warning("apply_refactor: refactor %s expired (%.0fs old)", refactor_id, age)
        return {"status": "error", "error": f"Refactor '{refactor_id}' has expired."}

    edits = preview.get("edits", [])
    if not edits:
        if dry_run:
            return {
                "status": "ok", "dry_run": True, "applied": 0,
                "files_modified": [], "edits_applied": 0,
                "would_modify": [], "diffs": {},
            }
        return {"status": "ok", "applied": 0, "files_modified": [], "edits_applied": 0}

    # --- Path traversal validation ---
    for edit in edits:
        edit_path = Path(edit["file"]).resolve()
        try:
            edit_path.relative_to(repo_root)
        except ValueError:
            logger.error(
                "apply_refactor: path traversal blocked for %s (repo_root=%s)",
                edit_path, repo_root,
            )
            return {
                "status": "error",
                "error": f"Edit path '{edit['file']}' is outside repo root.",
            }

    # --- Compute new content for every edit (shared by dry-run and write paths) ---
    # Group edits by file so multiple edits to the same file apply
    # sequentially against the updated content rather than stomping each
    # other. Dry-run and write modes then share this computation.
    from collections import defaultdict
    edits_by_file: dict[str, list[dict]] = defaultdict(list)
    for edit in edits:
        edits_by_file[edit["file"]].append(edit)

    planned: dict[str, tuple[str, str, int]] = {}  # file -> (old_content, new_content, edit_count)
    for file_str, file_edits in edits_by_file.items():
        file_path = Path(file_str)
        if not file_path.is_file():
            logger.warning("apply_refactor: file not found: %s", file_path)
            continue
        try:
            original = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("apply_refactor: could not read %s: %s", file_path, exc)
            continue

        content = original
        file_edits_applied = 0
        for edit in file_edits:
            old_text = edit["old"]
            new_text = edit["new"]
            if old_text not in content:
                logger.warning(
                    "apply_refactor: old text %r not found in %s",
                    old_text, file_path,
                )
                continue
            target_line = edit.get("line")
            if target_line is not None:
                lines = content.splitlines(keepends=True)
                idx = target_line - 1
                if 0 <= idx < len(lines) and old_text in lines[idx]:
                    lines[idx] = lines[idx].replace(old_text, new_text, 1)
                    content = "".join(lines)
                else:
                    content = content.replace(old_text, new_text, 1)
            else:
                content = content.replace(old_text, new_text, 1)
            file_edits_applied += 1

        if file_edits_applied > 0:
            planned[file_str] = (original, content, file_edits_applied)

    # --- Dry-run path: return diffs, no writes ---
    if dry_run:
        import difflib
        diffs: dict[str, str] = {}
        for file_str, (original, new_content, _count) in planned.items():
            diff_lines = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{file_str}",
                tofile=f"b/{file_str}",
                n=3,
            ))
            diffs[file_str] = "".join(diff_lines)
        total_edits = sum(count for _o, _n, count in planned.values())
        result = {
            "status": "ok",
            "dry_run": True,
            "applied": 0,
            "edits_applied": total_edits,
            "would_modify": sorted(planned.keys()),
            "files_modified": [],
            "diffs": diffs,
        }
        logger.info(
            "apply_refactor: dry-run %s — %d edits would be applied to %d files",
            refactor_id, total_edits, len(planned),
        )
        # Do NOT pop the pending refactor — let the user commit via a
        # second call with dry_run=False.
        return result

    # --- Real-write path: write the pre-computed new content ---
    files_modified: set[str] = set()
    edits_applied = 0
    for file_str, (_original, new_content, count) in planned.items():
        file_path = Path(file_str)
        try:
            file_path.write_text(new_content, encoding="utf-8")
            edits_applied += count
            files_modified.add(str(file_path))
            logger.info("apply_refactor: applied %d edit(s) to %s", count, file_path)
        except OSError as exc:
            logger.error("apply_refactor: could not write %s: %s", file_path, exc)

    # Remove from pending after successful application.
    with _refactor_lock:
        _pending_refactors.pop(refactor_id, None)

    result = {
        "status": "ok",
        "applied": edits_applied,
        "files_modified": sorted(files_modified),
        "edits_applied": edits_applied,
    }
    logger.info("apply_refactor: completed %s — %d edits applied", refactor_id, edits_applied)
    return result
