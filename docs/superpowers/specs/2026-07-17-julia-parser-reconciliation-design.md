# Julia Parser Reconciliation Design

## Context

Pull request #560 adds useful Julia coverage, but its head
`f66721a6f63cc352ea515ddf9ca6e9cba21c4666` is stale and conflicts with
current `main`. Current `main` already parses Julia modules, structs, long- and
short-form functions, imports, includes, exports/public symbols, macros,
enums, and testsets. The remaining source behavior is function stubs,
operators, parameterized constant aliases, aliased imports, one-line call
bodies, and complete module qualifiers.

The source implementation stores qualifiers only in `extra`. That preserves
display metadata but does not change graph identity or call targets. As a
result, a local `show` can collide with `Base.show`, a qualified call can be
reduced to `show`, and downstream graph queries cannot distinguish them.
Current nested Julia modules and functions also lose their outer lexical scope.

The source work is credited to Dan (`danvinci`, `danvinci@fastmail.net`). The
port commit will preserve that recognized attribution. Source PR #560 remains
untouched.

## Goals

- Port the safe, non-overlapping Julia behavior from PR #560.
- Give qualified Julia definitions collision-free canonical identities.
- Resolve bare and qualified calls to the nearest matching lexical symbol.
- Preserve complete nested module and function scopes through persistence.
- Record real modules for aliased imports and normalize calls through aliases.
- Keep malformed or unsupported Julia syntax fail-soft and free of bogus nodes.
- Preserve existing macros, enums, testsets, exports, includes, and Julia tests.

## Non-goals

- Replacing the Julia parser or changing the graph schema.
- Modeling Julia method dispatch by argument types or multiple dispatch.
- Creating nodes for ordinary scalar `const` bindings.
- Inventing definitions for external modules that are not present in the file.
- Supporting malformed qualified function stubs that the bundled grammar emits
  only as `ERROR` nodes.
- Merging, closing, reopening, or commenting on source PR #560.

## Canonical scope and containment

The implementation reuses the existing `NodeInfo.parent_name` identity model.
For Julia only, lexical scopes are joined instead of replacing each other:

- a function `f` in `Outer.Inner` has parent `Outer.Inner` and qualified name
  `file.jl::Outer.Inner.f`;
- a nested function `g` inside that function has parent `Outer.Inner.f` and
  qualified name `file.jl::Outer.Inner.f.g`;
- `function Base.show` written inside `Outer.Inner` has parent
  `Outer.Inner.Base`, name `show`, and qualified name
  `file.jl::Outer.Inner.Base.show`.

The explicit qualifier remains in `extra["julia_module_qualifier"]` for
consumers that need it directly. A qualified definition's `CONTAINS` source is
still its lexical module (`file.jl::Outer.Inner`), not a synthetic `Base` node.
This preserves source structure while making identity collision-free.

Nested `module` definitions recurse with their complete lexical path. Nested
functions and testsets likewise use their enclosing function path, so every
persisted `CONTAINS`, `CALLS`, and `TESTED_BY` endpoint refers to the same
qualified name as its node.

## Julia AST helpers

Small Julia-only helpers handle grammar shapes without changing generic
language behavior:

- flatten nested `field_expression` nodes in source order;
- read the final identifier or quoted operator component;
- split a field into qualifier and leaf name;
- find the callable inside signature wrappers such as `where_expression` and
  `typed_expression` with bounded traversal;
- join lexical scopes without duplicating path segments;
- read import aliases from `import_alias` nodes.

Every helper returns `None` or an empty result for an unknown shape. It does
not index children without checking them and does not recover definitions from
Tree-sitter `ERROR` nodes.

## Definitions and imports

Long- and short-form qualified definitions use the canonical scope described
above. Bare and quoted operators use the operator text as the function name,
including short forms such as `+(a, b) = a` and `Base.:+(a, b) = a`. A stub
such as `function hook end` becomes a normal Function node when Tree-sitter
provides a valid `function_definition`.

A `const` assignment becomes a Type node only when its right-hand side is a
parameterized/curly type expression, such as
`const FloatVec = Vector{Float64}`. Value constants remain on the existing
generic path.

Aliased Julia imports emit dependencies on the real imported module or symbol:
`import DataFrames as DF` records `DataFrames`, and selected aliases retain the
selected symbol path. The file-scope alias map records the local alias so a
qualified call through that alias can be normalized to the real module path.

## Call extraction and resolution

Qualified calls retain their dotted callee target instead of collapsing to a
leaf. For example, `LinearAlgebra.BLAS.gemv(x)` initially targets
`LinearAlgebra.BLAS.gemv` and records
`extra["julia_call_module"] = "LinearAlgebra.BLAS"`. An alias head is replaced
with its real import path before resolution.

The post-parse same-file resolver builds scoped Julia symbol keys from the
canonical node identities. For each unresolved Julia call it searches from the
caller's nearest parent scope outward, then checks the file-level symbol. Thus
a bare `f()` inside `Outer.Inner.g` prefers `Outer.Inner.f` over `Outer.f`, and
`Base.show()` can resolve to `Outer.Inner.Base.show` in the same lexical module.
Unmatched external calls remain stable dotted targets.

Julia qualifier `REFERENCES` edges are not rewritten as local functions. This
prevents a definition named `Base` elsewhere in the file from changing the
meaning of the qualifier reference.

Short-form definitions dispatch a right-hand side call node directly before
recursing into its children. This captures `delegate(x) = greet(x)` without
revisiting the left-hand signature as a self-call.

## Testing

Implementation proceeds in witnessed red-green cycles in a dedicated Julia
test module:

1. function stubs and malformed qualified-stub fail-soft behavior;
2. bare, quoted, qualified, and multi-segment operator definitions;
3. parameterized constant aliases versus scalar constants;
4. top-level and selected aliased imports plus alias-qualified calls;
5. one-line right-hand calls;
6. long/short qualified definitions, local-name collisions, and complete
   multi-segment call targets;
7. nested modules, nested functions, nearest-scope calls, macros, and testsets;
8. a `full_build`/`GraphStore` integration proving distinct persisted nodes,
   canonical call targets, and downstream caller lookup.

The existing Julia fixture tests run after each focused cycle. Final validation
includes the complete test suite, Ruff, type/schema/security checks used by CI,
diff inspection, graph change/flow review, and all GitHub checks (including
Windows) before the replacement PR is marked ready.
