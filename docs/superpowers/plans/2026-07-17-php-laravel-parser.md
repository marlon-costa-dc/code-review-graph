# PHP and Laravel Parser Port Implementation Plan

> **Execution:** Follow strict red/green/refactor order. Do not combine surfaces
> before the preceding focused test is green.

**Goal:** Port the viable PHP/Laravel behavior from closed PR #252 onto current
`main` while retaining stronger existing PHP calls/imports and adding the
required correctness, repository-boundary, and process-pool coverage.

**Architecture:** Extend the generic PHP syntax tables minimally. Keep Composer
resolution in bounded immutable helpers. Parse Blade through a dedicated
lightweight branch. Add Laravel edges in an independent PHP AST post-pass so
the generic recursive extractor remains unchanged.

**Source attribution:** The feature commit must include
`Co-authored-by: Minidoracat <minidora0702@gmail.com>`.

---

## Task 1: PHP syntax and PHP-only flow entry points

**Files:**

- Modify: `tests/test_multilang.py`
- Modify: `tests/test_flows.py`
- Modify: `code_review_graph/parser.py`
- Modify: `code_review_graph/flows.py`

1. Add focused tests for trait and enum Class nodes, `new` CALLS, PHP
   extends/implements targets, and unchanged existing scoped/member call
   formatting.
2. Add flow tests proving called PHP `boot`, `register`, and `__invoke`
   methods are entries while identically named Python methods are not.
3. Run both focused commands and confirm the new assertions fail for missing
   behavior:
   - `uv run --frozen --no-sync pytest -q tests/test_multilang.py -k PHP`
   - `uv run --frozen --no-sync pytest -q tests/test_flows.py -k php_entry`
4. Add only the PHP table, object-creation name, base-clause, and
   language-scoped flow changes.
5. Re-run the focused tests and refactor only while green.

## Task 2: Shape-safe, repository-bounded Composer PSR-4

**Files:**

- Create: `tests/test_php_laravel.py`
- Modify: `code_review_graph/parser.py`

1. Build temporary Composer repositories and add failing tests for:
   - normal PSR-4 resolution;
   - longest-prefix selection;
   - multiple directories for one prefix;
   - merged `autoload` and `autoload-dev` directories;
   - malformed document/section/`psr-4`/entry shapes;
   - caller and mapping paths outside the configured repository;
   - `..`, absolute-path, and symlink escapes;
   - compatibility fallback when Composer has no matching file.
2. Run:
   `uv run --frozen --no-sync pytest -q tests/test_php_laravel.py -k composer`
   and confirm failures are missing-resolution failures, not fixture errors.
3. Store a resolved repository root on `CodeParser`.
4. Add a bounded Composer ancestor search and immutable mapping loader.
   Validate JSON shapes, preserve all directories, merge sections, normalize
   prefixes, sort longest-first, and require resolved paths to stay within the
   root.
5. Integrate Composer before the existing PHP ancestor fallback.
6. Re-run the focused tests.

## Task 3: Composer cache and worker behavior

**Files:**

- Modify: `tests/test_php_laravel.py`
- Modify: `code_review_graph/parser.py`

1. Add failing cache tests proving:
   - independent parser instances reuse an unchanged Composer file;
   - a changed stat key reloads it;
   - repositories do not share cached mappings.
2. Add a bounded process-local cache keyed by Composer path, repository root,
   `mtime_ns`, and size, returning only immutable data.
3. Re-run Composer tests.

## Task 4: Blade parsing with comments and escapes

**Files:**

- Modify: `tests/test_php_laravel.py`
- Modify: `code_review_graph/parser.py`

1. Add failing tests for compound-extension detection, File node shape,
   `@extends`/`@include`/`@component` imports, `@livewire` references,
   exact line numbers, Blade-comment suppression, `@@` escape suppression,
   unterminated comments, invalid UTF-8, and ordinary PHP isolation.
2. Run:
   `uv run --frozen --no-sync pytest -q tests/test_php_laravel.py -k blade`.
3. Add Blade detection, comment masking that preserves newlines/offsets, and a
   negative-lookbehind directive matcher.
4. Re-run the focused tests.

## Task 5: Evidence-gated Laravel semantic edges

**Files:**

- Modify: `tests/test_php_laravel.py`
- Modify: `code_review_graph/parser.py`

1. Add positive failing tests for Route facade aliases/FQCNs, controller import
   aliases/FQCNs, Eloquent Model aliases/FQCNs, relationship targets, namespace
   blocks, and Composer-qualified graph targets.
2. Add negative failing tests for unrelated `Route` classes, missing facade
   imports, dynamic route handlers, non-Model classes, wrong receivers,
   similarly named methods, and non-`::class` arguments.
3. Assert each positive case still has the exact generic CALLS target already
   produced by `main`, with no duplicate generic edge.
4. Run:
   `uv run --frozen --no-sync pytest -q tests/test_php_laravel.py -k laravel`.
5. Add namespace-local PHP import bindings with aliases/grouped imports.
6. Add the standalone PHP semantic AST post-pass. Resolve semantic targets
   through Composer where possible; otherwise emit stable short targets.
7. Re-run the focused tests and inspect edge lists for duplicates.

## Task 6: Serial/process-pool parity

**Files:**

- Modify: `tests/test_php_laravel.py`

1. Add a Composer project with at least eight tracked PHP files.
2. Build it once with `CRG_SERIAL_PARSE=1` and once with the real process
   executor. Assert no errors and identical normalized nodes/edges.
3. Run the process test outside the restricted sandbox when OS semaphore
   access is required:
   `uv run --frozen --no-sync pytest -q tests/test_php_laravel.py -k process_pool`.

## Task 7: Documentation and source attribution

**Files:**

- Modify: `README.md`
- Modify: `docs/FEATURES.md`

1. Document Composer/Blade/Laravel support without claiming heuristic-only
   Route or Eloquent detection.
2. Run `git diff --check`.
3. Commit implementation and tests with the Minidoracat co-author trailer.

## Task 8: Verification, graph review, and publication

1. Run focused tests:
   `uv run --frozen --no-sync pytest -q tests/test_php_laravel.py tests/test_multilang.py tests/test_flows.py tests/test_incremental.py`.
2. Run all local CI-equivalent gates:
   - `uv run --frozen --no-sync ruff check code_review_graph/`
   - `uv run --frozen --no-sync --with mypy --with types-networkx mypy code_review_graph/ --ignore-missing-imports --no-strict-optional`
   - `uv run --frozen --no-sync --with 'bandit[toml]' bandit -r code_review_graph/ -c pyproject.toml`
   - a portable local equivalent of the schema-sync comparison in
     `.github/workflows/ci.yml`
   - `uv run --frozen --no-sync pytest --tb=short -q --cov=code_review_graph --cov-report=term-missing --cov-fail-under=65`
3. Use the repository graph to detect changed risk, affected flows, and test
   coverage; inspect any high-risk context.
4. Fetch and rebase latest `origin/main`, rerun focused/full gates, and
   confirm `git diff --check`.
5. Push `codex/port-php-laravel`, open a draft PR (CI runs only for pushes to
   `main` or pull requests), wait for the full PR CI to pass, then mark it
   ready.
6. Do not change source PR #252. Update/close bead `crg-erd.2.1` only after
   the ready PR and required CI are confirmed.
