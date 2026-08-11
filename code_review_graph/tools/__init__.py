"""Lazy public exports for Code Review Graph MCP tool implementations.

The package exposes tool functions for callers and tests that patch
``code_review_graph.tools.<name>``. Keep those names public without importing
every tool module on package import; several optional tool families import
heavy libraries that short read-only paths do not need.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

<<<<<<< HEAD
_EXPORTS: dict[str, tuple[str, str]] = {
    "_BUILTIN_CALL_NAMES": ("code_review_graph.tools._common", "_BUILTIN_CALL_NAMES"),
    "_get_store": ("code_review_graph.tools._common", "_get_store"),
    "_validate_repo_root": ("code_review_graph.tools._common", "_validate_repo_root"),
    "apply_refactor_func": ("code_review_graph.tools.refactor_tools", "apply_refactor_func"),
    "build_or_update_graph": ("code_review_graph.tools.build", "build_or_update_graph"),
    "cross_repo_search_func": ("code_review_graph.tools.registry_tools", "cross_repo_search_func"),
    "detect_changes_func": ("code_review_graph.tools.review", "detect_changes_func"),
    "embed_graph": ("code_review_graph.tools.docs", "embed_graph"),
    "find_large_functions": ("code_review_graph.tools.query", "find_large_functions"),
    "generate_wiki_func": ("code_review_graph.tools.docs", "generate_wiki_func"),
    "get_affected_flows_func": ("code_review_graph.tools.review", "get_affected_flows_func"),
    "get_architecture_overview_func": (
        "code_review_graph.tools.community_tools",
        "get_architecture_overview_func",
    ),
    "get_bridge_nodes_func": ("code_review_graph.tools.analysis_tools", "get_bridge_nodes_func"),
    "get_changed_files": ("code_review_graph.incremental", "get_changed_files"),
    "get_community_func": ("code_review_graph.tools.community_tools", "get_community_func"),
    "get_docs_section": ("code_review_graph.tools.docs", "get_docs_section"),
    "get_flow": ("code_review_graph.tools.flows_tools", "get_flow"),
    "get_hub_nodes_func": ("code_review_graph.tools.analysis_tools", "get_hub_nodes_func"),
    "get_impact_radius": ("code_review_graph.tools.query", "get_impact_radius"),
    "get_knowledge_gaps_func": (
        "code_review_graph.tools.analysis_tools",
        "get_knowledge_gaps_func",
    ),
    "get_minimal_context": ("code_review_graph.tools.context", "get_minimal_context"),
    "get_review_context": ("code_review_graph.tools.review", "get_review_context"),
    "get_staged_and_unstaged": ("code_review_graph.incremental", "get_staged_and_unstaged"),
    "get_suggested_questions_func": (
        "code_review_graph.tools.analysis_tools",
        "get_suggested_questions_func",
    ),
    "get_surprising_connections_func": (
        "code_review_graph.tools.analysis_tools",
        "get_surprising_connections_func",
    ),
    "get_wiki_page_func": ("code_review_graph.tools.docs", "get_wiki_page_func"),
    "list_communities_func": ("code_review_graph.tools.community_tools", "list_communities_func"),
    "list_flows": ("code_review_graph.tools.flows_tools", "list_flows"),
    "list_graph_stats": ("code_review_graph.tools.query", "list_graph_stats"),
    "list_repos_func": ("code_review_graph.tools.registry_tools", "list_repos_func"),
    "parse_diff_ranges": ("code_review_graph.changes", "parse_diff_ranges"),
    "parse_git_diff_ranges": ("code_review_graph.changes", "parse_git_diff_ranges"),
    "parse_svn_diff_ranges": ("code_review_graph.changes", "parse_svn_diff_ranges"),
    "query_graph": ("code_review_graph.tools.query", "query_graph"),
    "refactor_func": ("code_review_graph.tools.refactor_tools", "refactor_func"),
    "run_postprocess": ("code_review_graph.tools.build", "run_postprocess"),
    "semantic_search_nodes": ("code_review_graph.tools.query", "semantic_search_nodes"),
    "traverse_graph_func": ("code_review_graph.tools.query", "traverse_graph_func"),
}
=======
# -- _common ----------------------------------------------------------------
from ._common import (
    _BUILTIN_CALL_NAMES,
    _get_store,
    _validate_repo_root,
    with_provenance,
)
>>>>>>> upstream/main

__all__ = sorted(_EXPORTS)


<<<<<<< HEAD
def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
=======
# -- community_tools --------------------------------------------------------
from .community_tools import (
    get_architecture_overview_func,
    get_community_func,
    list_communities_func,
)

# -- context ----------------------------------------------------------------
from .context import get_minimal_context

# -- docs -------------------------------------------------------------------
from .docs import embed_graph, generate_wiki_func, get_docs_section, get_wiki_page_func

# -- flows_tools ------------------------------------------------------------
from .flows_tools import get_flow, list_flows

# -- query ------------------------------------------------------------------
from .query import (
    find_large_functions,
    get_impact_radius,
    list_graph_stats,
    query_graph,
    semantic_search_nodes,
    traverse_graph_func,
)

# -- refactor_tools ---------------------------------------------------------
from .refactor_tools import apply_refactor_func, refactor_func

# -- registry_tools ---------------------------------------------------------
from .registry_tools import cross_repo_search_func, list_repos_func

# -- review -----------------------------------------------------------------
from .review import (
    detect_changes_func,
    get_affected_flows_func,
    get_review_context,
)

__all__ = [
    # _common
    "_BUILTIN_CALL_NAMES",
    "_get_store",
    "_validate_repo_root",
    "with_provenance",
    # build
    "build_or_update_graph",
    "run_postprocess",
    # context
    "get_minimal_context",
    # community_tools
    "get_architecture_overview_func",
    "get_community_func",
    "list_communities_func",
    # docs
    "embed_graph",
    "generate_wiki_func",
    "get_docs_section",
    "get_wiki_page_func",
    # flows_tools
    "get_flow",
    "list_flows",
    # query
    "find_large_functions",
    "get_impact_radius",
    "list_graph_stats",
    "query_graph",
    "semantic_search_nodes",
    "traverse_graph_func",
    # refactor_tools
    "apply_refactor_func",
    "refactor_func",
    # registry_tools
    "cross_repo_search_func",
    "list_repos_func",
    # review
    "detect_changes_func",
    "get_affected_flows_func",
    "get_review_context",
    # analysis_tools
    "get_bridge_nodes_func",
    "get_hub_nodes_func",
    "get_knowledge_gaps_func",
    "get_suggested_questions_func",
    "get_surprising_connections_func",
    # re-exported for backward compat (used in test patches)
    "get_changed_files",
    "get_staged_and_unstaged",
    "parse_git_diff_ranges",
    "parse_svn_diff_ranges",
    "parse_diff_ranges",
]
>>>>>>> upstream/main
