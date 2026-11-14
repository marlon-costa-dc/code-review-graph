# Fork integration

`main` mirrors upstream. Merge the upstream tip into a dedicated candidate
based on `dc-use`, and merge that candidate through a reviewed PR. Never
rewrite either integration branch. The package in this candidate is
`2.4.0+dc.1`; a host-tools pin may refer only to the published release SHA.

Upstream owns graph storage, schema migration, worktree and shared HTTP
serving, the watcher lifecycle, and documentary skill generation. The fork
retains its host-facing watch manifest, repository daemon settings, bundle
store and prune surface, caller-specified review bounds, lean MCP tool
selection, and release assets. Generation is `make gen`; lock upgrades are
`make upg`; Ruff, Pyrefly, and upstream mypy run through `make lint`,
`make types`, and `make mypy` respectively.
