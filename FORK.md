# dc-use fork delta

This repository is the `dc-use` fork of
[`tirth8205/code-review-graph`](https://github.com/tirth8205/code-review-graph),
kept current with upstream `main` and carrying deployment-driven deltas. The
fork is distributed through the ai-hub host-tools pin (branch `dc-use`, exact
SHA recorded in the host-tools manifest). Fork releases use PEP 440 local
versions derived from the current upstream release: upstream is at `2.3.8`
and the fork is at `2.3.8+dc.3`. The next fork release is `2.3.8+dc.4` while
upstream stays at `2.3.8`; when upstream releases a new version, the fork
rebases onto it and restarts the local suffix at `+dc.1`. Fork version
numbers are never invented ahead of an upstream release.

## Why the fork exists

The upstream engine needed production hardening and automation seams that
upstream does not carry yet. Every delta below exists to serve the ai-hub CRG
autopilot (ai-hub ADR-0026); anything upstream adopts can be dropped from
this list at the next sync.

## Delta themes (57 commits on `dc-use` over upstream `main`)

- **Correctness / fail-loud**: `serve` startup crash from a duplicated
  `detail_level` keyword fixed; parse errors fail builds; fallback paths,
  a shadowed-method layer in `graph.py`, and the duplicate-detector in
  `refactor.py` were removed; atomic graph writes (`graph.py`,
  `incremental.py`, `migrations.py`).
- **Performance**: authoritative VCS file inventory via `git ls-files`;
  batched per-file graph writes, existence checks, signature updates;
  SQLite pragma tuning on graph and embedding connections; vectorized
  embedding search with numpy; hook hot-path optimization.
- **Embeddings**: local sentence-transformers runtime aligned across the
  major-version upgrade; registry isolation; lazy embedding load with
  `prewarm_local_embeddings()` so model loading never runs inside FastMCP
  executor threads (deadlock fix); cloud providers gated behind explicit
  acceptance.
- **Analysis accuracy**: Jedi-based Python call resolution wired into builds;
  MRO-correct dead-code and edge-layer inheritance resolution; enum and
  namespace-class dead-code heuristics; community and flow detection
  improvements; graph-backed refactor previews.
- **MCP surface**: lean curated tool set (`--tools lean` / `CRG_TOOLS=lean`);
  token-budgeted review output; bounded query results; graph-not-built guard
  for read tools.
- **Runtime**: `doctor` health checklist; FastMCP 4 / MCP 2 migration;
  one-line uv installer; Beads integration for this repository's own
  development; VSCode reader schema alignment.

## Automation contract (consumed by ai-hub)

| Surface | Contract |
| --- | --- |
| Submodule recursion | `CRG_RECURSE_SUBMODULES=1` makes `git ls-files --recurse-submodules` feed the graph; off by default |
| Data placement | `CRG_DATA_DIR` externalizes the graph database; `CRG_HOME` relocates registry, watch config, logs; registry entries carry per-repo `data_dir` |
| Embeddings | `CRG_EMBEDDING_MODEL` selects the local model; cloud providers require explicit acceptance env |
| Serving | `CRG_TOOLS=lean`, `CRG_DETAIL_LEVEL`, `CRG_TOOL_TIMEOUT` bound the MCP surface |
| Watch daemon | `crg-daemon` start/stop/status with health records, restart backoff, hot config reload |

Known gaps scheduled under the autopilot epic (ai-hub beads `aihub-3t7yh.3`):
per-repo daemon options in `watch.toml` (only `path`/`alias` today), no
seed/reuse mechanism for worktree indexes, no `prune` command, and no
worktree-aware indexing.

## Sync policy

`dc-use` fast-forwards upstream `main` regularly; fork deltas are rebased on
top by merge from `main` (never history rewrite of `dc-use`). The deployed
SHA is always pushed before the host-tools manifest references it.
