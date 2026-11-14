SHELL := /bin/sh

.PHONY: setup gen upg lint fix types mypy mod test test-release

setup:
	uv sync --locked --extra dev

gen:
	uv run --locked --extra dev python scripts/generate_documentary_skills.py
	uv run --locked --extra dev python -c 'from pathlib import Path; from code_review_graph.skills import generate_skills; generate_skills(Path.cwd(), Path("skills"))'

upg:
	uv lock --upgrade

lint:
	uv run --locked --extra dev ruff check code_review_graph tests

fix:
	uv run --locked --extra dev ruff check --fix code_review_graph tests

types:
	uv run --locked --extra dev pyrefly check code_review_graph

mypy:
	uv run --locked --extra dev mypy code_review_graph --ignore-missing-imports --no-strict-optional

mod:
	flext-infra refactor mod --repository-root .

test:
	uv run --locked --extra dev pytest tests

test-release:
	uv run --locked --extra dev pytest tests/test_release_provenance.py -q --tb=short

.PHONY: test-notebooks
test-notebooks:
	uv run --locked --extra dev pytest tests/test_notebook.py -q --tb=short

.PHONY: test-skills
test-skills:
	uv run --locked --extra dev pytest tests/test_skill_template_schema_sync.py tests/test_skills.py -q --tb=short

.PHONY: test-daemon
test-daemon:
	uv run --locked --extra dev pytest tests/test_daemon.py tests/test_daemon_external_graph.py tests/test_prune.py -q --tb=short

.PHONY: test-worktrees
test-worktrees:
	uv run --locked --extra dev pytest tests/test_multi_worktree.py -q --tb=short

.PHONY: test-watch-locks
test-watch-locks:
	uv run --locked --extra dev pytest tests/test_write_lock.py tests/test_write_lock_all_writers.py -q --tb=short

.PHONY: test-freshness
test-freshness:
	uv run --locked --extra dev pytest tests/test_freshness_metadata.py tests/test_tools.py -q --tb=short

.PHONY: test-install
test-install:
	uv run --locked --extra dev pytest tests/test_cli_install.py tests/test_steering_hook.py tests/test_installer_ownership.py tests/test_install_outputs_are_ignored.py -q --tb=short
