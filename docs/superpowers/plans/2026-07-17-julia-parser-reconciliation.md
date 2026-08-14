# Julia Parser Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the safe Julia parser behavior from PR #560 while giving nested and module-qualified definitions collision-free identities and resolvable downstream graph edges.

**Architecture:** Keep the existing parser and graph schema. Julia-only AST helpers derive field components, operator names, aliases, and complete lexical scopes; parser nodes encode lexical scope plus explicit qualifier in `parent_name`, while post-parse call resolution searches from the caller's nearest scope outward.

**Tech Stack:** Python 3.10+, Tree-sitter via `tree-sitter-language-pack`, pytest, SQLite `GraphStore`, Ruff, mypy, Bandit.

## Global Constraints

- Do not change the graph schema or generic behavior for other languages.
- Do not infer definitions from Tree-sitter `ERROR` nodes.
- Do not turn scalar Julia `const` bindings into Type nodes.
- Keep source PR #560 unchanged and credit `dan <danvinci@fastmail.net>` in the implementation commit.
- Every production change must follow a witnessed red-green-refactor cycle.
- Preserve all existing Julia macros, enums, testsets, exports, includes, and fixture tests.

---

## File structure

- Create `tests/test_julia_reconciliation.py`: focused parser, scope, fail-soft,
  persistence, and downstream query regressions independent of the shared
  multi-language fixture.
- Modify `code_review_graph/parser.py`: Julia-only helpers and extraction,
  scope, import-alias, call-target, and same-file resolution behavior.
- Keep `tests/fixtures/sample.jl` and `tests/test_multilang.py` unchanged so
  they remain an independent no-regression check of existing Julia behavior.

### Task 1: Julia leaf constructs and fail-soft extraction

**Files:**
- Create: `tests/test_julia_reconciliation.py`
- Modify: `code_review_graph/parser.py:3361-3695,7431-7475,7909-7950`

**Interfaces:**
- Consumes: `CodeParser.parse_bytes(Path, bytes) -> tuple[list[NodeInfo], list[EdgeInfo]]`.
- Produces: `_julia_component_name(node) -> Optional[str]`,
  `_julia_field_parts(node) -> list[str]`,
  `_julia_field_info(node) -> tuple[Optional[str], Optional[str]]`, and valid
  Function/Type/import outputs for the source constructs.

- [ ] **Step 1: Write focused failing tests**

Create a helper that writes no production files and parses snippets directly:

```python
from pathlib import Path

import pytest

from code_review_graph.parser import CodeParser


def _parse(source: str):
    return CodeParser().parse_bytes(Path("/repo/case.jl"), source.encode())


def test_function_stub_is_a_function():
    nodes, _ = _parse("function hook end")
    assert [(n.kind, n.name) for n in nodes if n.kind != "File"] == [
        ("Function", "hook"),
    ]


@pytest.mark.parametrize("signature", ["+(a, b) = a", "Base.:+(a, b) = a"])
def test_operator_definition_uses_operator_name(signature):
    nodes, _ = _parse(signature)
    functions = [n for n in nodes if n.kind == "Function"]
    assert [n.name for n in functions] == ["+"]


def test_parameterized_const_only_is_a_type():
    nodes, _ = _parse(
        "const FloatVec = Vector{Float64}\nconst MAX_RETRIES = 3\n"
    )
    assert {n.name for n in nodes if n.kind == "Type"} == {"FloatVec"}


def test_import_alias_records_real_dependency():
    _, edges = _parse(
        "import DataFrames as DF\nimport Tables: AbstractColumns as Columns\n"
    )
    assert {e.target for e in edges if e.kind == "IMPORTS_FROM"} == {
        "DataFrames",
        "Tables.AbstractColumns",
    }


def test_malformed_qualified_stub_fails_soft():
    nodes, edges = _parse("function A.B.hook end")
    assert [n.kind for n in nodes] == ["File"]
    assert edges == []
```

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run --frozen --no-sync pytest -q \
  tests/test_julia_reconciliation.py::test_function_stub_is_a_function \
  tests/test_julia_reconciliation.py::test_operator_definition_uses_operator_name \
  tests/test_julia_reconciliation.py::test_parameterized_const_only_is_a_type \
  tests/test_julia_reconciliation.py::test_import_alias_records_real_dependency \
  tests/test_julia_reconciliation.py::test_malformed_qualified_stub_fails_soft
```

Expected: stub, operator, type-alias, and import-alias assertions fail for
missing behavior; malformed input already passes and guards the implementation.

- [ ] **Step 3: Implement minimal Julia leaf handling**

Add helpers that return `None` for unknown shapes, use them from
`_julia_short_func_name` and Julia `_get_name`, add a `const_statement` branch
to `_extract_julia_constructs`, and extend `_extract_import` for direct and
selected `import_alias` nodes. The concrete scope-free helper contract is:

```python
@staticmethod
def _julia_component_name(node) -> Optional[str]:
    if node.type in ("identifier", "operator"):
        return node.text.decode("utf-8", errors="replace")
    if node.type == "quote_expression":
        for child in node.children:
            name = CodeParser._julia_component_name(child)
            if name is not None:
                return name
    if node.type == "parenthesized_expression":
        for child in node.children:
            if child.type == "operator":
                return child.text.decode("utf-8", errors="replace")
    return None
```

For a parameterized const, append `NodeInfo(kind="Type", ...)` and a matching
`CONTAINS` edge, then return `True`. Do not consume any other const statement.

- [ ] **Step 4: Verify GREEN and no Julia regressions**

Run:

```bash
uv run --frozen --no-sync pytest -q \
  tests/test_julia_reconciliation.py tests/test_multilang.py::TestJuliaParsing
```

Expected: all selected tests pass.

### Task 2: Canonical qualified identities and scoped call resolution

**Files:**
- Modify: `tests/test_julia_reconciliation.py`
- Modify: `code_review_graph/parser.py:2423-2460,3361-3695,4889-5225,5257-5360`

**Interfaces:**
- Consumes: Julia field helpers from Task 1.
- Produces: `_julia_scope_join(left, right) -> Optional[str]`,
  `_julia_definition_qualifier(node) -> Optional[str]`, canonical parent names,
  dotted call targets, and nearest-scope resolution in `_resolve_call_targets`.

- [ ] **Step 1: Write collision and call failures**

Add tests using a module that defines local `show`, `Base.show`,
`Base.length`, `Base.:+`, `A.B.run`, a one-line delegate, and dotted calls.
Assert these exact identities and edges:

```python
assert ("show", "Demo") in identities
assert ("show", "Demo.Base") in identities
assert ("length", "Demo.Base") in identities
assert ("+", "Demo.Base") in identities
assert ("run", "Demo.A.B") in identities
assert ("Demo.delegate", "Demo.show") in call_tails
assert ("Demo.caller", "Demo.A.B.run") in call_tails
assert any(
    e.target == "LinearAlgebra.BLAS.gemv"
    and e.extra["julia_call_module"] == "LinearAlgebra.BLAS"
    for e in calls
)
```

Also assert the qualifier reference from `Demo.Base.show` targets literal
`Base`, even when the file contains a local function named `Base`.

- [ ] **Step 2: Verify RED**

Run the new collision/call tests individually with `pytest -vv`. Expected:
qualified identities collapse under `Demo`, `Base.:+` becomes a bogus `Base`
function, one-line calls are missing, and dotted targets remain bare leaves.

- [ ] **Step 3: Implement canonical scope and qualified calls**

Use this identity rule in long and short definitions:

```python
lexical_parent = self._julia_scope_join(enclosing_class, enclosing_func)
identity_parent = self._julia_scope_join(lexical_parent, qualifier)
```

When `qualifier` is absent, `identity_parent` is `lexical_parent`. Store the
explicit qualifier in `extra["julia_module_qualifier"]`; create `CONTAINS` from
`lexical_parent`; recurse into the function with
`enclosing_class=identity_parent` and `enclosing_func=name`.

In `_extract_calls`, replace a Julia field-expression callee with its complete
`qualifier.leaf` text and set `julia_call_module`. Dispatch a short-form RHS
call node directly before descending into its children.

In `_resolve_call_targets`, retain the current implementation for non-Julia
files. For Julia, index every definition by the tail of its canonical
qualified name and test candidates in this order for source
`file::Outer.Inner.caller` and target `f`: `Outer.Inner.caller.f`,
`Outer.Inner.f`, `Outer.f`, `f`. Skip local rewriting for REFERENCES edges
whose extra contains `julia_qualified_def`.

- [ ] **Step 4: Verify GREEN**

Run the new tests plus `tests/test_multilang.py::TestJuliaParsing`; expected:
all pass and no existing Julia assertion changes.

### Task 3: Nested scopes, aliases, macros, and testsets

**Files:**
- Modify: `tests/test_julia_reconciliation.py`
- Modify: `code_review_graph/parser.py:3630-3695,4889-5020,6707-6760`

**Interfaces:**
- Consumes: canonical scope and resolver from Task 2.
- Produces: full lexical paths for nested modules/functions/testsets and Julia
  alias bindings in `import_map`.

- [ ] **Step 1: Write nested and alias failures**

Add a nested `Outer.Inner` snippet with shadowed `f`, a nested function, a
function-local `@testset`, `@inline`/ordinary macro calls, and
`import DataFrames as DF`. Assert:

```python
assert "Outer.Inner" in class_parents_and_names
assert ("f", "Outer.Inner") in identities
assert ("leaf", "Outer.Inner.wrapper") in identities
assert any(e.target.endswith("::Outer.Inner.f") for e in calls_from_inner)
assert any(n.kind == "Test" and n.parent_name == "Outer.Inner.wrapper" for n in nodes)
assert any(e.target == "DataFrames.transform" for e in alias_calls)
assert any(e.target == "@inline" for e in calls)
```

- [ ] **Step 2: Verify RED**

Run only these tests. Expected: nested functions/modules use truncated parent
names, the nearest call picks `Outer.f`, the testset identity differs from its
edge source, and the alias call remains `DF.transform`.

- [ ] **Step 3: Implement nested scope and alias normalization**

For Julia class/module extraction, recurse with the joined scope and emit
`CONTAINS` from the enclosing scope rather than always from the File. For
testsets, use joined lexical function scope as `parent_name`, containment
source, and recursion scope.

Extend `_collect_import_names` for Julia aliases so:

```python
import_map["DF"] = "DataFrames"
import_map["Columns"] = "Tables.AbstractColumns"
```

When a qualified call's first module segment is an alias, replace it before
forming the final dotted target. Leave direct modules unchanged.

- [ ] **Step 4: Verify GREEN and refactor**

Run the focused file and existing Julia class. Remove duplicate AST walks only
after all tests pass, then rerun the same command.

### Task 4: GraphStore and downstream query persistence

**Files:**
- Modify: `tests/test_julia_reconciliation.py`

**Interfaces:**
- Consumes: `full_build(Path, GraphStore) -> dict` and canonical parser output.
- Produces: an integration regression proving the SQLite node and edge API sees
  the same identities as the parser.

- [ ] **Step 1: Write the full-build test**

Create `.git` and a Julia source under `tmp_path`, run `full_build`, then assert:

```python
local = store.get_node(f"{source_path}::Demo.show")
base = store.get_node(f"{source_path}::Demo.Base.show")
assert local is not None and base is not None and local.id != base.id

callers = store.get_edges_by_target(f"{source_path}::Demo.Base.show")
assert any(edge.kind == "CALLS" and edge.source.endswith("::Demo.invoke")
           for edge in callers)
assert result["errors"] == []
```

Also query the persisted nested target and Type alias, and close the store in a
`finally` block.

- [ ] **Step 2: Verify RED or prove prior task coverage**

Run this test before any integration-specific production change. It must fail
against pre-port production code; if Tasks 1-3 already make it pass, temporarily
revert the parser changes, confirm the expected identity/call failure, restore
them, and rerun green.

- [ ] **Step 3: Verify focused and full suites**

Run:

```bash
uv run --frozen --no-sync pytest -q tests/test_julia_reconciliation.py \
  tests/test_multilang.py::TestJuliaParsing
uv run --frozen --no-sync pytest -q
```

Expected baseline delta: all 1,573 prior passes remain green plus the new Julia
tests; existing skips/xpasses remain non-failures.

### Task 5: Static checks, graph review, attribution, rebase, and publication

**Files:**
- Verify: `code_review_graph/parser.py`
- Verify: `tests/test_julia_reconciliation.py`
- Verify: design and plan documents

**Interfaces:**
- Produces: a ready replacement PR based on the latest `origin/main`, with Dan
  credited and source PR #560 unchanged.

- [ ] **Step 1: Run local quality gates**

```bash
uv run --frozen --no-sync ruff check code_review_graph tests
uv run --frozen --no-sync ruff format --check code_review_graph tests
uv run --frozen --no-sync mypy code_review_graph
uv run --frozen --no-sync bandit -q -r code_review_graph
uv run --frozen --no-sync python scripts/check_schema_sync.py
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 2: Review the graph and diff**

Incrementally rebuild the knowledge graph, then run change detection, affected
flows, tests-for queries, and focused review context against `origin/main`.
Inspect `git diff --stat`, `git diff`, and ensure only the approved files and
behavior changed.

- [ ] **Step 3: Commit with attribution**

Commit production/tests with this trailer:

```text
Co-authored-by: dan <danvinci@fastmail.net>
```

- [ ] **Step 4: Rebase and repeat fresh verification**

Fetch `origin/main`, rebase the branch, rerun focused/full tests and every
static check, update the graph, and inspect the final diff. Resolve no unrelated
changes and never force-push without explicit approval.

- [ ] **Step 5: Push and open a ready replacement PR**

The PR body must name source PR #560 and its exact head, enumerate overlap and
ported behavior, explain the extra-only collision/call-resolution blocker,
state the exact base/head and local test evidence, credit Dan, and say source
PR #560 was not modified. Wait for every required check, including Windows, to
finish successfully before reporting readiness.
