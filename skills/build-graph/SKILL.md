---
name: build-graph
description: Build, update, post-process, watch, register and health-check the code knowledge graph
---

## Build Graph

Keep the graph current before any graph-backed answer. A stale graph reports facts about code that no longer exists.

### Check freshness

1. `code-review-graph status` prints `Built on branch` and `Built at commit`; compare them with `git rev-parse HEAD`.
2. `code-review-graph doctor` checks graph presence, freshness, MCP config, serve command, server import, hooks and embeddings. It exits non-zero only when the graph is missing or empty or the MCP server does not import; freshness, MCP config and hooks are warnings, so read every line, not only the exit code.

### Build or update

- `code-review-graph update` (MCP: `build_or_update_graph_tool()`) re-parses files changed since the commit the graph was built at, plus their dependents. Use it after ordinary edits, commits and branch switches.
- `code-review-graph build` (MCP: `build_or_update_graph_tool(full_rebuild=True)`) re-parses every tracked file. Use it for a missing graph and after history rewrites or mass moves.
- Submodule workspaces: `CRG_RECURSE_SUBMODULES=1 code-review-graph build` feeds `git ls-files --recurse-submodules`, so submodule files enter the graph; without the variable only the superproject is indexed. Set it for every command that builds that graph. `update` diffs the superproject only: an edit or commit inside a submodule is not re-parsed, yet `status` reports the new commit. After submodule changes, run the recursive `build` again.
- `--skip-flows` (signatures and search index only) and `--skip-postprocess` (raw parse only) write faster but leave flows and communities stale.

### Post-process

`code-review-graph postprocess` (MCP: `run_postprocess_tool`) recomputes derived data on the existing graph: signatures, the full-text search index, execution flows and communities (`--no-flows`, `--no-communities`, `--no-fts` skip one step). Run it before flow, community or architecture queries whenever the last write skipped post-processing, including the editor hook, which runs `update --skip-flows`. Embeddings are separate: `code-review-graph embed` (MCP: `embed_graph_tool`) uses the local provider by default; a cloud provider transmits source-derived text.

### Watch

- `code-review-graph watch` keeps one repository current in the foreground; `serve --auto-watch` does the same inside the MCP server; `code-review-graph daemon start|status|add|remove` supervises watchers for the repositories in its watch config.
- Run one watcher per graph. Before starting one, check `code-review-graph daemon status` and any host supervisor that owns the graph (under ai-hub: `systemctl --user status ai-hub-watch.service`, whose orphan sweep reconciles loose watcher processes). When one is active, use `update` for an immediate refresh; never start a second watcher.

### Multi-repo registry

- `code-review-graph register <path> --alias <name>`, `repos` and `unregister <path-or-alias>` manage `registry.json` under `~/.code-review-graph` (relocated by `CRG_HOME`).
- `list_repos_tool` lists registered repositories; `cross_repo_search_tool(query=...)` searches every registered graph. Registration does not build: each repository still needs its own graph.
- `code-review-graph prune` reports dead registry and watch entries; `--apply` removes them.

### MCP server and hooks

- `code-review-graph install` registers the `code-review-graph` MCP server for detected platforms, writes skills and instructions, and installs hooks (`--no-hooks`, `--no-skills`, `--no-instructions`, `--dry-run`, `--platform`). A client may expose a tool subset (`serve --tools lean|<csv>`, `CRG_TOOLS`) or load MCP tool schemas on demand; load a schema before its first call.
- Claude Code hooks: `PostToolUse` on Edit/Write runs `update --skip-flows`; `SessionStart` runs `status`. The git `pre-commit` hook runs `update` and `detect-changes --brief` from the directory git uses for hooks (`core.hooksPath` is respected).
- `doctor` looks for hooks only in `.claude/settings.json`, `.qoder/settings.json` and `.git/hooks/pre-commit`; with `core.hooksPath` set, a working git hook is reported as missing.

### Failures

- Refresh a stale or missing graph with the commands above, then repeat the query; never answer from a stale graph or guess missing relationships.
- A failed build, update or post-process is a failure: report the command, exit code and error.

## Token Efficiency Rules
- Start with `get_minimal_context_tool(task="<your task>")` before other graph tools.
- Use `detail_level="minimal"` on all calls. Only escalate to "standard" when minimal is insufficient.
- Target: complete any review/debug/refactor task in ≤5 tool calls and ≤800 total output tokens.
- Read the implementation and its tests before changing code. The graph narrows scope; it does not replace the source.
