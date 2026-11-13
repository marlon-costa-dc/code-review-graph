# Architecture

## System Overview

`code-review-graph` is a local-first code intelligence graph exposed through a CLI and MCP server. It maintains a persistent, incrementally updated knowledge graph of a codebase so AI coding tools can review changes with structural context instead of reading broad file dumps. Claude Code is supported, but it is one client among several.

## Component Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                    AI coding clients / CLI                    │
│                                                              │
│  MCP clients              Hooks / watch mode                 │
│  ├── Codex                └── incremental update             │
│  ├── Claude Code, CodeBuddy Code                             │
│  ├── Cursor, Windsurf, Zed, Continue                         │
│  └── Gemini CLI, Qwen, Qoder, Copilot, OpenCode              │
│          │                        │                          │
│          ▼                        ▼                          │
│  ┌────────────────────────────────────────────┐              │
│  │      MCP Server (stdio or localhost HTTP)  │              │
│  │                                            │              │
│  │  30 MCP Tools + 5 MCP Prompts              │              │
│  │  ├── Core: build, impact, query, review,   │              │
│  │  │   search, traverse, embed, stats, docs  │              │
│  │  ├── Flows: list, get, affected            │              │
│  │  ├── Communities: list, get, architecture  │              │
│  │  ├── Analysis: detect_changes, refactor,   │              │
│  │  │   apply_refactor, hotspots, gaps        │              │
│  │  ├── Wiki: generate, get_page              │              │
│  │  └── Multi-repo: list_repos, cross_search  │              │
│  └────────────────┬───────────────────────────┘              │
└───────────────────┼──────────────────────────────────────────┘
                    │
        ┌───────────┼───────────────┐
        ▼           ▼               ▼
   ┌─────────┐ ┌─────────┐  ┌─────────────┐
   │ Parser  │ │  Graph  │  │ Incremental │
   │         │ │  Store  │  │   Engine    │
   └────┬────┘ └────┬────┘  └──────┬──────┘
        │           │              │
        ▼           ▼              ▼
   Tree-sitter   SQLite DB      git/svn diff
   grammars      (.code-review- subprocess
                 graph/
                 graph.db)
```

## Data Flow

### Full Build
1. `collect_all_files()` gathers tracked files (`git ls-files`) and applies `.code-review-graphignore` (gitignored files are skipped automatically when git is available)
2. For each file, `CodeParser.parse_file()` uses Tree-sitter to extract AST
3. AST walker identifies structural nodes (classes, functions, imports) and edges (calls, inheritance)
4. `GraphStore.store_file_nodes_edges()` persists to SQLite with file hash for change detection
5. Metadata updated with timestamp

### Incremental Update
1. `get_changed_files()` uses VCS metadata to identify changed files (git diff by default, with SVN support in the incremental layer)
2. Content hashes discard unchanged notification seeds before any expansion.
   Read failures remain fatal. `GraphStore.get_dependent_files()` expands all
   changed-content and stale file seeds together through recorded imports, calls,
   inheritance and implementation edges.
   Each breadth-first frontier uses indexed SQLite queries in batches of 450 paths;
   visited files terminate cycles and every original seed is excluded from the
   dependent result, including cyclic and multi-seed queries. Incremental updates
   do not truncate by file count or depth. Query failures propagate before publication.
3. Changed files and the complete recorded dependent closure are checked. Hashes
   skip unchanged files while repository-wide resolvers refresh their semantic
   relationships. Permanent deletion removes incoming edges owned by direct
   dependents, so those files are re-parsed even when their bytes are identical.
   Transitive dependents whose evidence remains intact keep their unchanged rows.
   Dependent paths must remain lexically and physically under the repository
   root before parsing or publication. Ordinary nested Git submodules remain
   eligible; an outside row or symlink escape fails rather than indexing another
   checkout through this graph.
4. Affected rows are published atomically after parsing succeeds. Dependency
   expansion completes before parsing, writes or freshness metadata updates.
   This proves completeness over recorded graph edges, not dynamic dependencies
   that static analysis has never indexed.

Explicit `forget` captures the affected surviving referrers before deleting
anything, then replaces the selected files with no nodes or edges through the
store's existing atomic batch operation. The connection uses explicit transactions;
per-file `remove_file_data` calls otherwise commit each statement separately.
That old path timed out during removal of 10,674 executor files from the Algar
graph and exposed partially removed rows. Referrer reconstruction, derived
index processing and orphan-vector accounting retain their existing owners after
the atomic deletion; the replacement operation does not purge vectors early.
An existing caller-owned transaction is rejected before processing any target;
`forget` neither commits nor discards that pending work.

An explicit `max_hops` requests a bounded query from the same store owner; deletion
uses one hop to identify rows that must be reconstructed. Complete incremental
selection never uses that bound. The unused `find_dependents()` forwarding wrapper,
500-file truncation and partial-list flag are retired together with their consumers.
Regression validation uses isolated graphs: first enumerate the intended source
changes and affected closure without writes, then compare the resulting incremental
graph with a separately rebuilt graph. Live registered graphs and watchers are not
validation fixtures.

The regression cohort in `tests/test_incremental.py::TestMultiHopDependents`
parses 601 actual importers and compares API rename/deletion results with a
separate full rebuild, including a five-file chain, unchanged source bytes and
unchanged-notification metadata. Run it without the repository's default worker
fan-out:

```bash
rtk proxy env PYTEST_ADDOPTS='-n0 -s' make test FILE=tests/test_incremental.py MATCH=TestMultiHopDependents
```

On the isolated 602-file TypeScript cohort, closure discovery took 0.010–0.014
seconds. A rename updated one file in 0.219 seconds versus a 5.407-second rebuild;
deletion required 601
unchanged importers to reconstruct their removed edges and took 4.703 seconds
versus a 3.196-second rebuild. These host-dependent samples establish the work
performed, not a universal speedup. Read-only `EXPLAIN QUERY PLAN` confirmed
target/kind lookups through `idx_edges_target_kind` and symbol-file lookups
through `idx_nodes_file`; the wider covering index otherwise scanned by kind.

Watch startup starts the empty observer once, then schedules live emitters through
watchdog's public API. A directory proven absent during scheduling is reconciled
as a deletion; other scheduling errors escape. Retrying `Observer.start()` is
invalid because watchdog may already have started some emitter threads. Events
queue while the initial build and derived-state processing converge, and the
debouncer starts afterward so it cannot mutate the graph during that build.
Its standard queue retains events received before the worker starts; consuming
the queue precedes the debounce wait. An unconditional initial condition wait
would lose the early notification and leave a write made during initial
post-processing unindexed until another filesystem event arrived.
The worker ends after a failed batch; the watch boundary raises the recorded
failure before another source batch can publish.
Clean shutdown drains accepted events and propagates a failure discovered during
that drain. A batch window is bounded from its first event by the existing
`_DEBOUNCE_SECONDS`; continuous edits cannot postpone publication indefinitely.
The production handler has one queued writer path. Test consumers exercise that
same lifecycle instead of a synchronous method that bypasses serialization.
Observer teardown propagates the real operation exception or timeout. The
dispatcher must be confirmed stopped before the queue receives its shutdown
sentinel. An existing primary failure retains priority when cleanup also fails;
cleanup failure never turns the invocation green.
Liveness uses the native thread identity as proof that start completed. A reader
that starts and dies between supervision ticks, including a replacement reader,
must be classified as a death; only a thread never started is still pending.
Health publication is mandatory for the public watcher: path/import and
filesystem failures propagate instead of disabling reports. Startup refuses an
unusable health directory before graph publication, and cleanup retains the
first failure while reporting its own failure explicitly.
The configured heartbeat interval limits routine writes; a failed observer
publishes the stalled state immediately even within that interval.
`watch(..., observer=...)` accepts an unused native watchdog `BaseObserver` as
an explicit host dependency. The watcher owns its complete schedule/start/stop
lifecycle. A supplied observer with an existing thread identity or registered
emitters is rejected before health publication or graph mutation; omission uses
the platform's native `Observer`. This permits host-controlled backend ownership
and real failure/recovery acceptance without another execution path.
Watcher acceptance shares one real lifecycle fixture: native observer events,
independent SQLite readback, bounded shutdown and original failure propagation.
An empty unchanged repository invokes no derived-state callback; deleting an
actual competing Python module must re-resolve imports through the live event
pipeline. Incremental enrichment acceptance requires a genuine content change,
since an unchanged hash deliberately performs no enrichment work.
The native lock resolves Starlette's corrected `BlockingPortal` annotation
against AnyIO 4.15, retaining the Python 3.10-compatible Starlette release for
that interpreter. The upstream [Starlette defect](https://github.com/Kludex/starlette/issues/3497)
is repaired by dependency resolution, never by suppressing its import warning.

Graph publication, scoped-call resolution and impact reporting retain one owner
for each repeated effect. Metadata classification chooses the target and extra
fields first; one update path publishes that decision and synchronizes test
mirrors. Empty impact requests use the same empty seed result as unmatched files.
Real-observer regressions share lifecycle setup and cleanup while retaining
their distinct source mutations and observable graph assertions. Adoption and
replacement tests wait for the `watching` phase before changing the filesystem;
the earlier heartbeat only proves that the initial build has started. A new
top-level directory must acquire its own recursive emitter and reach the graph.
An ignored top-level directory must receive neither an emitter nor graph rows,
even when a sibling is adopted during the same watch cycle.
Directories created beneath an existing recursive emitter are indexed through
that emitter without allocating a redundant descendant watch.
Recreation is exercised both after the original emitter is released and within
one watcher tick, preserving deletion and reindexing evidence in each case.

Atomic file replacement and permanent removal publish through
`apply_file_changes_atomic()`. Their active single-file/removal operations keep
their current contracts and delegate transaction ownership; the unused batch
entry point is retired with its bound test consumers. Both impact engines share
one report assembler, retaining raw namespace bridge seeds as well as material
nodes, exact truncation/count evidence, edge selection and score precision.

Native Mypy profiling retains the official roots and flags. The recorded
`--timing-stats` run inspected 6,940 modules in 30.50 seconds; 1,610 Torch modules
accounted for 20.204 seconds of per-module work. The following verbose run took
25.99 seconds and identified a 1,280-module Torch component as inherently stale
because of `torch.utils.cpp_extension`. This is evidence of the dependency/cache
closure, not permission to skip those imports or suppress diagnostics. SQLite
and fixed-format caches are already enabled by the installed checker.
Startup failures stop only their own observer, clear their health evidence and
restore the installed signal handler before propagating the original failure.
Watch-tree reconciliation dispatches adopted/deleted directory events to that
same debouncer. Directory contents are enumerated when the batch runs, after
the new watch is installed. The heartbeat thread never indexes an adopted tree
or invokes derived-state callbacks concurrently with the batch processor.

### Review Context Generation
Generated agent instructions share one compact source-verification guardrail.
Its rendered sentences stay intact across platforms and retain the original
source and test verification requirements within the existing context budget.

1. Changed files identified (git diff or explicit list)
2. `get_impact_radius()` walks outward from the changed nodes, relaxing a weighted
   best-path score per node (per-edge-kind weight × depth decay) rather than a plain BFS
3. Source snippets extracted for changed areas only
4. Review guidance generated (test coverage gaps, wide blast radius warnings)
5. Assembled into a structured, token-efficient context for MCP clients and the CLI
6. Where a cheap baseline can be estimated, compact `context_savings` metadata is attached as an estimate rather than an exact tokenisation

## Storage

### SQLite Schema
- **nodes** table: id, kind, name, qualified_name, file_path, line_start/end, language, community_id, etc.
- **edges** table: id, kind, source_qualified, target_qualified, file_path, line
- **metadata** table: key-value pairs (last_updated, build_type, schema_version)
- **flows** table: id, name, entry_point_id, depth, node_count, file_count, criticality, path_json
- **flow_memberships** table: flow_id, node_id, position
- **communities** table: id, name, level, parent_id, cohesion, size, dominant_language, description
- **nodes_fts** (FTS5 virtual table): full-text search on name, qualified_name, file_path, signature
- **community_summaries**, **flow_snapshots**, **risk_index** tables: compact precomputed summaries for token-efficient queries
- **embeddings** table (separate DB): qualified_name, vector, text_hash, provider

Indexes on qualified_name, file_path, edge source/target, criticality, community_id, and cohesion for fast lookups.

WAL mode enabled for concurrent read access during updates.

### Qualified Names
Nodes are uniquely identified by qualified names:
- Files: absolute path (e.g., `/repo/src/auth.py`)
- Functions: `file_path::function_name` (e.g., `/repo/src/auth.py::authenticate`)
- Methods: `file_path::ClassName.method_name` (e.g., `/repo/src/auth.py::AuthService.login`)

## Parsing Strategy

Tree-sitter provides language-agnostic AST access. The parser:
1. Walks the AST recursively
2. Pattern-matches on node types (language-specific mappings in `_CLASS_TYPES`, `_FUNCTION_TYPES`, etc.)
3. Extracts names, parameters, return types, base classes
4. Identifies calls within function bodies
5. Resolves imports to module paths

This approach is more robust than tree-sitter queries across grammar versions.

## Visualization

The `visualization.py` module generates an interactive D3.js force-directed graph as a self-contained HTML file. It reads all nodes and edges from the SQLite graph store and renders them in the browser, allowing developers to visually explore code relationships, filter by node kind, and inspect dependencies.

## Impact Analysis Algorithm

BFS from seed nodes (changed files' contents):
1. Seed = all qualified names in changed files
2. For each node in frontier:
   - Follow forward edges (what this node affects)
   - Follow reverse edges (what depends on this node)
3. Expand up to `max_depth` hops (default: 2)
4. Collect all reached nodes as "impacted"

This captures both downstream effects (things that call changed code) and upstream context (things that the changed code depends on).
