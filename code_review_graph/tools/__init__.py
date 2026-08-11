"""Lazy public exports for Code Review Graph MCP tool implementations.

The package exposes tool functions for callers and tests that patch
``code_review_graph.tools.<name>``. Keep those names public without importing
every tool module on package import; several optional tool families import
heavy libraries that short read-only paths do not need.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from ..changes import parse_diff_ranges, parse_git_diff_ranges, parse_svn_diff_ranges
from ..incremental import get_changed_files, get_staged_and_unstaged

# -- _common ----------------------------------------------------------------
from ._common import (
    _BUILTIN_CALL_NAMES,
    _get_store,
    _validate_repo_root,
    with_provenance,
)

from .analysis_tools import (
    get_bridge_nodes_func,
    get_hub_nodes_func,
    get_knowledge_gaps_func,
    get_suggested_questions_func,
    get_surprising_connections_func,
)

from .build import build_or_update_graph, run_postprocess

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
