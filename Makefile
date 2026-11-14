SHELL := /bin/sh
.PHONY: setup tools-lock tools-setup check test test-concurrency graph graph-query mod rules-test rules-snapshots

setup:
	uv sync --frozen --extra dev

tools-lock:
	uv lock --project .tools
	mise trust .mise.toml
	mise lock --jobs 2

tools-setup:
	uv sync --project .tools --frozen
	mise install --locked --yes aqua:ast-grep/ast-grep

check:
	uv run --frozen ruff check code_review_graph/
	uv run --frozen mypy code_review_graph/ --ignore-missing-imports --no-strict-optional

test:
	uv run --frozen pytest tests/test_daemon.py tests/test_daemon_external_graph.py --tb=short -q

test-concurrency:
	uv run --frozen pytest tests/test_concurrency_and_daemon.py -m concurrency --tb=short -q

graph:
	uv run --frozen code-review-graph build --repo .

graph-query:
	uv run --frozen code-review-graph query tests_for "$(CURDIR)/code_review_graph/daemon.py::WatchDaemon" --repo .

mod:
	.tools/.venv/bin/flext-infra refactor mod --repository-root . --rule crg-daemon-native-graph-path

rules-test:
	ast-grep test --config config/rules/ast-grep/sgconfig.yml

rules-snapshots:
	ast-grep test --config config/rules/ast-grep/sgconfig.yml --update-all
