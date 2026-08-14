# PHP and Laravel Parser Port Design

## Context

Pull request #252 contains useful PHP, Composer, Blade, and Laravel parsing
work, but its branch predates stronger PHP call and `use` handling that is
already on `main`. This port takes only the remaining behavior and preserves
the current generic parser path.

The source implementation is credited to Minidoracat
(`minidora0702@gmail.com`). The port commit will retain that attribution.

## Goals

- Recognize PHP traits, enums, object creation, and inheritance/interface
  clauses without changing existing PHP call or import formatting.
- Treat PHP `boot`, `register`, and `__invoke` methods as language-scoped
  entry points. Existing universal `handle`, `up`, and `down` behavior
  remains unchanged.
- Resolve PHP namespaces through Composer PSR-4 mappings safely and
  deterministically.
- Parse Blade template references while ignoring Blade comments and escaped
  directives.
- Add Laravel Route-to-controller and Eloquent relationship edges only when
  the AST contains explicit framework and receiver evidence.
- Keep serial and process-pool builds behaviorally equivalent.

## Non-goals

- Replacing the existing PHP parser or import resolver.
- Inferring Laravel semantics from method names alone.
- Supporting every Blade directive or dynamic route target.
- Resolving Composer dependencies outside the repository.
- Reopening, merging, or otherwise modifying source PR #252.

## Design

### PHP syntax and entry points

The existing Tree-sitter tables gain PHP `trait_declaration`,
`enum_declaration`, and `object_creation_expression`. The current PHP
`_get_call_name` branch is extended only for object creation, and
`_get_bases` gains PHP `base_clause` and `class_interface_clause`
handling.

Flow detection gains a PHP-only pattern set for `boot`, `register`, and
`__invoke`. Names that are already universal are not duplicated.

### Composer PSR-4 resolution

`CodeParser` records its resolved repository root. Composer lookup starts at
the caller directory and stops at that root, inclusive. If no root was supplied,
the parser uses the nearest VCS root; without a safe boundary it does not climb
above the caller directory.

Composer data is accepted only when each container has the expected JSON shape:

- document, `autoload`, and `autoload-dev`: objects;
- `psr-4`: object;
- prefix: string;
- mapped path: string or list of strings.

Mappings from both sections are combined without overwriting. All valid mapped
directories are retained in declaration order. Prefixes are normalized and
searched longest-first. Resolved base directories and target files must remain
inside the repository after symlink resolution; absolute paths and `..`
escapes that leave the repository are ignored.

The parsed mapping is immutable. A bounded process-local cache is keyed by the
Composer path, repository root, file modification time, and size. This lets
serial parsers and long-lived process-pool workers reuse configuration without
stale cross-repository results.

The current ancestor-walk resolver remains as a compatibility fallback after
Composer resolution.

### Blade templates

Compound `.blade.php` names are detected before ordinary `.php` suffix
handling. A dedicated lightweight parser emits one File node and:

- `IMPORTS_FROM` for `@extends`, `@include`, and `@component`;
- `REFERENCES` for `@livewire`.

Blade comment spans (`{{-- ... --}}`) are masked while preserving newlines and
character offsets. The directive matcher requires an unescaped `@`, so
`@@include` and equivalent escaped forms do not emit edges. Edge line numbers
therefore remain aligned with the original source.

### Laravel semantic evidence

Laravel analysis runs as a separate PHP AST post-pass after generic extraction.
It never consumes a Tree-sitter node and never recreates the ordinary CALLS
edge. This preserves current targets such as `Route::get`, `hasMany`, and
all nested calls.

The post-pass builds namespace-local class import bindings, including aliases
and grouped imports, and tracks the enclosing class.

A Route controller CALLS edge is emitted only when:

1. the scoped call is a supported route verb;
2. its receiver is an alias imported from
   `Illuminate\\Support\\Facades\\Route`, or that full class name is used;
3. the handler has the static array form
   `[Controller::class, 'method']`.

An Eloquent REFERENCES edge is emitted only when:

1. the call uses a supported relationship method;
2. the receiver is exactly `$this`;
3. the enclosing class extends an imported/fully-qualified
   `Illuminate\\Database\\Eloquent\\Model`;
4. the first relevant argument is `Target::class`.

Imported or fully-qualified controller/model names are resolved through
Composer. When a target file exists, the semantic edge uses the graph's real
qualified-name shape (`file.php::Class.method` for routes and
`file.php::Class` for models). Otherwise it retains a stable short semantic
target rather than inventing a file.

## Testing

Each surface is implemented red-first:

1. PHP traits, enums, `new`, base clauses, and language-scoped entry points.
2. Composer malformed shapes, longest prefix, multi-directory mappings,
   `autoload-dev` merging, cache invalidation/reuse, repository traversal,
   absolute paths, and symlink escape.
3. Blade directives, line numbers, comments, escaped directives, malformed
   input, and ordinary PHP isolation.
4. Laravel positive alias/FQCN cases and negative unrelated `Route`,
   non-model relationship, wrong receiver, dynamic handler, and missing-import
   cases.
5. Serial/process-pool parity on a Composer PHP project with enough files to
   enter the parallel path.

After focused tests, the full suite, Ruff, schema generation check, graph change
review, and GitHub CI must pass before the ready PR is opened.
