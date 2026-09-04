SHELL := /bin/sh

WHAT ?= all
FILE ?=
MATCH ?=
APPLY ?= N
REPO ?= .

.PHONY: help setup deps check test profile

help:
	@printf '%s\n' \
	  'code-review-graph' \
	  '  setup' \
	  '  deps WHAT=check|lock APPLY=Y' \
	  '  check WHAT=all|lint|mypy|duplication' \
	  '  test [FILE=<path>] [MATCH=<pytest-expression>]' \
	  '  profile FILE=<profile-output> REPO=<repository>'

setup:
	uv sync --all-extras

deps:
	@case "$(WHAT)" in \
	  check) uv lock --check ;; \
	  lock) test "$(APPLY)" = Y || { echo 'ERROR: deps WHAT=lock requires APPLY=Y' >&2; exit 2; }; uv lock ;; \
	  *) echo 'ERROR: WHAT must be check|lock' >&2; exit 2 ;; \
	esac

check:
	@set -eu; case "$(WHAT)" in \
	  all) uv run ruff check code_review_graph tests; uv run mypy code_review_graph --ignore-missing-imports --no-strict-optional; $(MAKE) check WHAT=duplication ;; \
	  lint) uv run ruff check $(if $(FILE),$(FILE),code_review_graph tests) ;; \
	  mypy) uv run mypy $(if $(FILE),$(FILE),code_review_graph) --ignore-missing-imports --no-strict-optional ;; \
	  duplication) npx --yes jscpd@5.1.2 --min-lines 8 --mode strict --reporters ai --summary code_review_graph tests ;; \
	  *) echo 'ERROR: WHAT must be all|lint|mypy|duplication' >&2; exit 2 ;; \
	esac

test:
	uv run pytest $(if $(FILE),$(FILE),tests) $(if $(MATCH),-k $(MATCH),)

profile:
	@test -n "$(FILE)" || { echo 'ERROR: FILE must name the cProfile output' >&2; exit 2; }
	uv run python -m cProfile -o "$(FILE)" scripts/profile_inventory.py "$(REPO)"
