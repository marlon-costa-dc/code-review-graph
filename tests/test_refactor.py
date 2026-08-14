"""Tests for graph-powered refactoring operations."""

import tempfile
import threading
import time
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser, EdgeInfo, NodeInfo
from code_review_graph.refactor import (
    REFACTOR_EXPIRY_SECONDS,
    _is_test_file,
    _pending_refactors,
    _refactor_lock,
    apply_refactor,
    find_dead_code,
    rename_preview,
    suggest_refactorings,
)


class TestRenamePreview:
    """Tests for rename_preview."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)
        # Clean up pending refactors.
        with _refactor_lock:
            _pending_refactors.clear()

    def _seed(self):
        """Seed the store with test data for rename tests."""
        # File nodes
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/utils.py", file_path="/repo/utils.py",
            line_start=1, line_end=50, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/main.py", file_path="/repo/main.py",
            line_start=1, line_end=30, language="python",
        ))
        # Function to rename
        self.store.upsert_node(NodeInfo(
            kind="Function", name="helper", file_path="/repo/utils.py",
            line_start=10, line_end=20, language="python",
        ))
        # Caller function
        self.store.upsert_node(NodeInfo(
            kind="Function", name="run", file_path="/repo/main.py",
            line_start=5, line_end=15, language="python",
        ))
        # CALLS edge: run -> helper
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/main.py::run",
            target="/repo/utils.py::helper", file_path="/repo/main.py", line=10,
        ))
        # IMPORTS_FROM edge: main.py imports helper
        self.store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM", source="/repo/main.py",
            target="/repo/utils.py::helper", file_path="/repo/main.py", line=1,
        ))
        self.store.commit()

    def test_rename_preview_returns_edits_with_refactor_id(self):
        """rename_preview returns a dict with refactor_id and edits."""
        result = rename_preview(self.store, "helper", "new_helper")
        assert result is not None
        assert "refactor_id" in result
        assert len(result["refactor_id"]) == 8
        assert result["type"] == "rename"
        assert result["old_name"] == "helper"
        assert result["new_name"] == "new_helper"
        assert isinstance(result["edits"], list)
        assert len(result["edits"]) > 0
        assert "stats" in result
        assert result["stats"]["high"] > 0

    def test_rename_finds_callers(self):
        """rename_preview finds definition + call sites."""
        result = rename_preview(self.store, "helper", "new_helper")
        assert result is not None
        edits = result["edits"]
        # Should have at least: 1 definition + 1 call + 1 import = 3
        assert len(edits) >= 3
        files = {e["file"] for e in edits}
        assert "/repo/utils.py" in files  # definition
        assert "/repo/main.py" in files   # call site + import site

    def test_rename_bare_callers_use_js_family_without_crossing_to_apex(self):
        """A JS rename includes a TSX bare caller but not an Apex name collision."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="formatValue", file_path="/repo/format.js",
            line_start=1, line_end=5, language="javascript",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="tsxCaller", file_path="/repo/caller.tsx",
            line_start=1, line_end=5, language="tsx",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="apexCaller", file_path="/repo/Caller.cls",
            line_start=1, line_end=5, language="apex",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/caller.tsx::tsxCaller",
            target="formatValue", file_path="/repo/caller.tsx", line=3,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/Caller.cls::apexCaller",
            target="formatValue", file_path="/repo/Caller.cls", line=3,
        ))
        self.store.commit()

        result = rename_preview(self.store, "formatValue", "renderValue")

        assert result is not None
        edit_files = {edit["file"] for edit in result["edits"]}
        assert "/repo/caller.tsx" in edit_files
        assert "/repo/Caller.cls" not in edit_files

    def test_rename_not_found(self):
        """rename_preview returns None if symbol not found."""
        result = rename_preview(self.store, "nonexistent_function", "new_name")
        assert result is None

    def test_rename_stores_in_pending(self):
        """rename_preview stores the preview in _pending_refactors."""
        result = rename_preview(self.store, "helper", "new_helper")
        assert result is not None
        rid = result["refactor_id"]
        with _refactor_lock:
            assert rid in _pending_refactors


class TestFindDeadCode:
    """Tests for find_dead_code."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed(self):
        """Seed with a mix of used and unused functions."""
        # File
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/app.py", file_path="/repo/app.py",
            line_start=1, line_end=100, language="python",
        ))
        # A function that IS called
        self.store.upsert_node(NodeInfo(
            kind="Function", name="used_func", file_path="/repo/app.py",
            line_start=10, line_end=20, language="python",
        ))
        # A function that is NOT called (dead code)
        self.store.upsert_node(NodeInfo(
            kind="Function", name="dead_func", file_path="/repo/app.py",
            line_start=30, line_end=40, language="python",
        ))
        # An entry point function (should be excluded)
        self.store.upsert_node(NodeInfo(
            kind="Function", name="main", file_path="/repo/app.py",
            line_start=50, line_end=60, language="python",
        ))
        # A test function (should be excluded)
        self.store.upsert_node(NodeInfo(
            kind="Test", name="test_something", file_path="/repo/test_app.py",
            line_start=1, line_end=10, language="python", is_test=True,
        ))

        # Caller for used_func
        self.store.upsert_node(NodeInfo(
            kind="Function", name="caller", file_path="/repo/app.py",
            line_start=70, line_end=80, language="python",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/app.py::caller",
            target="/repo/app.py::used_func", file_path="/repo/app.py", line=75,
        ))
        self.store.commit()

    def test_find_dead_code(self):
        """find_dead_code detects unreferenced functions."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "dead_func" in dead_names

    def test_find_dead_code_response_fields(self):
        """dead_code entries include file_path, relative_path, and language."""
        dead = find_dead_code(self.store, root="/repo")
        entry = next(d for d in dead if d["name"] == "dead_func")
        assert entry["file_path"] == "/repo/app.py"
        assert entry["relative_path"] == "app.py"
        assert entry["language"] == "python"
        # backward compat: 'file' key still present
        assert entry["file"] == "/repo/app.py"

    def test_find_dead_code_relative_path_without_root(self):
        """Without root, relative_path falls back to file_path."""
        dead = find_dead_code(self.store)
        entry = next(d for d in dead if d["name"] == "dead_func")
        assert entry["relative_path"] == "/repo/app.py"

    def test_find_dead_code_excludes_called(self):
        """find_dead_code does NOT include functions with callers."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "used_func" not in dead_names

    def test_find_dead_code_excludes_entry_points(self):
        """Entry points (like 'main') are not flagged as dead code."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "main" not in dead_names

    def test_find_dead_code_excludes_tests(self):
        """Test nodes are not flagged as dead code."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "test_something" not in dead_names


    def test_is_test_file_recognizes_suffix_variants(self):
        """_is_test_file recognizes pytest suffix/prefix variants without over-matching."""
        assert _is_test_file("a/values_check_tests.py")
        assert _is_test_file("a/foo_tests.py")
        assert _is_test_file("a/foo_test.py")
        assert _is_test_file("a/conftest.py")
        assert _is_test_file("b/sub/bar_tests.py")
        assert not _is_test_file("src/real_module.py")
        assert not _is_test_file("a/contest.py")
        assert not _is_test_file("a/latest.py")
        assert not _is_test_file("a/attest.py")

    def test_find_dead_code_kind_filter(self):
        """kind filter restricts results."""
        dead = find_dead_code(self.store, kind="Class")
        # We have no Class nodes, so should be empty
        assert len(dead) == 0

    def test_find_dead_code_file_pattern(self):
        """file_pattern filter works."""
        dead = find_dead_code(self.store, file_pattern="nonexistent")
        assert len(dead) == 0

    def test_find_dead_code_excludes_dunder(self):
        """Dunder methods are not flagged as dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="__init__", file_path="/repo/app.py",
            line_start=90, line_end=95, language="python",
            parent_name="MyClass",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "__init__" not in dead_names

    def test_find_dead_code_excludes_constructor(self):
        """JS/TS constructors are not flagged as dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="constructor", file_path="/repo/component.ts",
            line_start=10, line_end=15, language="typescript",
            parent_name="MyComponent",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "constructor" not in dead_names

    def test_find_dead_code_excludes_angular_lifecycle(self):
        """Angular lifecycle hooks are not flagged as dead code."""
        for name in ("ngOnInit", "ngOnChanges", "ngOnDestroy", "transform",
                     "writeValue", "canActivate"):
            self.store.upsert_node(NodeInfo(
                kind="Function", name=name, file_path="/repo/component.ts",
                line_start=10, line_end=15, language="typescript",
                parent_name="MyComponent",
            ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        for name in ("ngOnInit", "ngOnChanges", "ngOnDestroy", "transform",
                     "writeValue", "canActivate"):
            assert name not in dead_names, f"{name} should not be dead"

    def test_find_dead_code_excludes_decorated_entry(self):
        """Functions with framework decorators are not flagged as dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="get_users", file_path="/repo/app.py",
            line_start=90, line_end=95, language="python",
            extra={"decorators": ["app.get('/users')"]},
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "get_users" not in dead_names

    def test_find_dead_code_excludes_type_referenced_class(self):
        """Classes referenced in function type annotations are not dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="UserSchema", file_path="/repo/app.py",
            line_start=5, line_end=15, language="python",
        ))
        # A function that uses UserSchema in its params
        self.store.upsert_node(NodeInfo(
            kind="Function", name="create_user", file_path="/repo/app.py",
            line_start=20, line_end=30, language="python",
            params="body: UserSchema",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "UserSchema" not in dead_names

    def test_find_dead_code_excludes_return_type_reference(self):
        """Classes referenced in return types are not dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="UserResponse", file_path="/repo/app.py",
            line_start=5, line_end=15, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="get_user", file_path="/repo/app.py",
            line_start=20, line_end=30, language="python",
            return_type="Optional[UserResponse]",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "UserResponse" not in dead_names

    def test_find_dead_code_excludes_orm_model(self):
        """Classes inheriting from known ORM bases are not dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="User", file_path="/repo/app.py",
            line_start=5, line_end=20, language="python",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="INHERITS", source="/repo/app.py::User",
            target="Base", file_path="/repo/app.py", line=5,
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "User" not in dead_names

    def test_find_dead_code_excludes_pydantic_settings(self):
        """Classes inheriting from BaseSettings are not dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="AppConfig", file_path="/repo/app.py",
            line_start=5, line_end=15, language="python",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="INHERITS", source="/repo/app.py::AppConfig",
            target="BaseSettings", file_path="/repo/app.py", line=5,
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "AppConfig" not in dead_names

    def test_find_dead_code_excludes_agent_tool(self):
        """Functions with @agent.tool decorator are not dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="query_data", file_path="/repo/app.py",
            line_start=10, line_end=20, language="python",
            extra={"decorators": ["health_agent.tool"]},
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "query_data" not in dead_names

    def test_find_dead_code_excludes_alembic_upgrade(self):
        """upgrade() and downgrade() in alembic files are not dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="upgrade", file_path="/repo/alembic/versions/001.py",
            line_start=5, line_end=15, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="downgrade", file_path="/repo/alembic/versions/001.py",
            line_start=20, line_end=30, language="python",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "upgrade" not in dead_names
        assert "downgrade" not in dead_names

    def test_find_dead_code_excludes_subclassed_class(self):
        """Classes with subclasses (INHERITS edges) are not dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="BaseConnector", file_path="/repo/connectors.py",
            line_start=5, line_end=50, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Class", name="GarminConnector", file_path="/repo/connectors.py",
            line_start=60, line_end=90, language="python",
        ))
        # A subclass inherits from BaseConnector (bare-name target)
        self.store.upsert_edge(EdgeInfo(
            kind="INHERITS", source="/repo/connectors.py::GarminConnector",
            target="BaseConnector", file_path="/repo/connectors.py", line=60,
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "BaseConnector" not in dead_names

    def test_find_dead_code_bare_calls_use_js_family_without_apex(self):
        """TS callers keep JS code live; same-named Apex calls do not."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="usedFromTs", file_path="/repo/shared.js",
            line_start=1, line_end=5, language="javascript",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="apexOnlyCollision", file_path="/repo/shared.js",
            line_start=10, line_end=15, language="javascript",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="tsCaller", file_path="/repo/caller.ts",
            line_start=1, line_end=5, language="typescript",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="apexCaller", file_path="/repo/Caller.cls",
            line_start=1, line_end=5, language="apex",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/caller.ts::tsCaller",
            target="usedFromTs", file_path="/repo/caller.ts", line=3,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/Caller.cls::apexCaller",
            target="apexOnlyCollision", file_path="/repo/Caller.cls", line=3,
        ))
        self.store.commit()

        dead_names = {item["name"] for item in find_dead_code(self.store)}

        assert "usedFromTs" not in dead_names
        assert "apexOnlyCollision" in dead_names

    def test_find_dead_code_bare_inheritance_uses_js_family_without_apex(self):
        """TS subclasses keep JS bases live; Apex subclasses do not."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="UsedJsBase", file_path="/repo/base.js",
            line_start=1, line_end=8, language="javascript",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Class", name="ApexOnlyBase", file_path="/repo/base.js",
            line_start=10, line_end=18, language="javascript",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Class", name="TsChild", file_path="/repo/child.ts",
            line_start=1, line_end=8, language="typescript",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Class", name="ApexChild", file_path="/repo/Child.cls",
            line_start=1, line_end=8, language="apex",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="INHERITS", source="/repo/child.ts::TsChild",
            target="UsedJsBase", file_path="/repo/child.ts", line=1,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="INHERITS", source="/repo/Child.cls::ApexChild",
            target="ApexOnlyBase", file_path="/repo/Child.cls", line=1,
        ))
        self.store.commit()

        dead_names = {item["name"] for item in find_dead_code(self.store)}

        assert "UsedJsBase" not in dead_names
        assert "ApexOnlyBase" in dead_names

    def test_find_dead_code_bare_name_not_tricked_by_unrelated_caller(self):
        """Bare-name CALLS from unrelated files don't save a dead function
        when there are multiple definitions with the same name."""
        # Two unrelated functions named "processor" in different files
        self.store.upsert_node(NodeInfo(
            kind="Function", name="processor", file_path="/repo/api/routes.py",
            line_start=10, line_end=20, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="processor", file_path="/repo/worker/tasks.py",
            line_start=10, line_end=20, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="start", file_path="/repo/main.py",
            line_start=1, line_end=20, language="python",
        ))
        # A bare CALLS edge from a third file that imports only routes.py
        self.store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM", source="/repo/main.py",
            target="/repo/api/routes.py", file_path="/repo/main.py", line=1,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/main.py::start",
            target="processor", file_path="/repo/main.py", line=10,
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_qnames = {d["qualified_name"] for d in dead}
        # routes.py processor is saved (caller imports its file)
        assert "/repo/api/routes.py::processor" not in dead_qnames
        # worker/tasks.py processor is dead (no relationship with caller)
        assert "/repo/worker/tasks.py::processor" in dead_qnames

    def test_find_dead_code_excludes_mock_variables(self):
        """Mock/stub variables in test files are not flagged as dead code."""
        for name in ("mockDynamoClient", "s3ClientMock", "MockService", "createMockRequest"):
            self.store.upsert_node(NodeInfo(
                kind="Function", name=name, file_path="/repo/tests/handler.spec.ts",
                line_start=10, line_end=15, language="typescript",
            ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        for name in ("mockDynamoClient", "s3ClientMock", "MockService", "createMockRequest"):
            assert name not in dead_names, f"{name} should not be dead (mock pattern)"

    def test_find_dead_code_excludes_angular_decorated_class(self):
        """Angular @Component classes are not flagged as dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="ClipboardButtonComponent",
            file_path="/repo/src/app/clipboard.component.ts",
            line_start=5, line_end=50, language="typescript",
            extra={"decorators": ["Component({selector: 'app-clipboard'})"]},
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "ClipboardButtonComponent" not in dead_names

    def test_find_dead_code_excludes_parsed_python_decorated_class(self):
        """Decorator metadata must survive parsing before dead-code analysis."""
        from code_review_graph.parser import CodeParser

        nodes, _ = CodeParser().parse_bytes(
            Path("/repo/widget.py"),
            b'@Component("widget-card")\nclass Widget:\n    pass\n',
        )
        widget = next(node for node in nodes if node.name == "Widget")
        self.store.upsert_node(widget)
        self.store.commit()

        dead_names = {item["name"] for item in find_dead_code(self.store)}
        assert "Widget" not in dead_names

    def test_find_dead_code_excludes_property(self):
        """Functions decorated with @property are not dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="db", file_path="/repo/deps.py",
            line_start=10, line_end=15, language="python",
            extra={"decorators": ["property"]},
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "db" not in dead_names


class TestFindDeadCodeNewHeuristics:
    """Regression tests for recently added false-positive filters."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_protocol_class_methods_not_dead(self):
        """Methods of typing.Protocol classes are interface contract, not dead."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="Greeter", file_path="/repo/protocols.py",
            line_start=1, line_end=10, language="python",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="INHERITS", source="/repo/protocols.py::Greeter",
            target="typing.Protocol", file_path="/repo/protocols.py", line=1,
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="greet", file_path="/repo/protocols.py",
            line_start=2, line_end=5, language="python", parent_name="Greeter",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "greet" not in dead_names

    def test_testcase_methods_not_dead(self):
        """Methods of unittest.TestCase subclasses are wired by the runner."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="MyTests", file_path="/repo/test_helpers.py",
            line_start=1, line_end=20, language="python",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="INHERITS", source="/repo/test_helpers.py::MyTests",
            target="unittest.TestCase", file_path="/repo/test_helpers.py", line=1,
        ))
        for name in ("setUp", "tearDown", "test_something"):
            self.store.upsert_node(NodeInfo(
                kind="Function", name=name, file_path="/repo/test_helpers.py",
                line_start=3, line_end=5, language="python", parent_name="MyTests",
            ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        for name in ("setUp", "tearDown", "test_something"):
            assert name not in dead_names, f"{name} should not be flagged dead"

    def test_pydantic_computed_field_not_dead(self):
        """Methods decorated with @computed_field are framework-managed."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="display_name", file_path="/repo/schemas.py",
            line_start=10, line_end=15, language="python",
            extra={"decorators": ["computed_field"]},
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "display_name" not in dead_names

    def test_orm_validates_not_dead(self):
        """Methods decorated with @orm.validates are SQLAlchemy event hooks."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="validate_email", file_path="/repo/models.py",
            line_start=20, line_end=25, language="python",
            extra={"decorators": ["orm.validates('email')"]},
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "validate_email" not in dead_names

    def test_django_permission_classes_not_dead(self):
        """Functions with DRF permission_classes decorator are API entry points."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="list_users", file_path="/repo/views.py",
            line_start=5, line_end=10, language="python",
            extra={"decorators": ["permission_classes([IsAuthenticated])"]},
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "list_users" not in dead_names

    def test_click_option_command_not_dead(self):
        """Functions decorated with click.option are CLI command definitions."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="hello", file_path="/repo/cli.py",
            line_start=10, line_end=15, language="python",
            extra={"decorators": ["click.option('--count')"]},
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "hello" not in dead_names

    def test_argparse_subcommand_not_dead(self, tmp_path):
        """Functions registered via ``set_defaults(func=...)`` are CLI entry points."""
        cli_file = tmp_path / "cli.py"
        cli_file.write_text(
            "import argparse\n"
            "def command_standard(args):\n"
            "    pass\n"
            "subparsers = argparse.ArgumentParser().add_subparsers()\n"
            "subparsers.add_parser('standard').set_defaults(func=command_standard)\n",
            encoding="utf-8",
        )
        self.store.upsert_node(NodeInfo(
            kind="Function", name="command_standard", file_path=str(cli_file),
            line_start=2, line_end=3, language="python",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "command_standard" not in dead_names

    def test_method_call_in_fstring_not_dead(self, tmp_path):
        """Methods invoked inside f-string interpolations are not dead code."""
        src_file = tmp_path / "app.py"
        src_file.write_text(
            "class PublishResult:\n"
            "    def to_tsv(self) -> str:\n"
            "        return ''\n"
            "def write(results):\n"
            "    f'{results[0].to_tsv()}\\n'\n",
            encoding="utf-8",
        )
        self.store.upsert_node(NodeInfo(
            kind="Class", name="PublishResult", file_path=str(src_file),
            line_start=1, line_end=3, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="to_tsv", file_path=str(src_file),
            line_start=2, line_end=3, language="python", parent_name="PublishResult",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "to_tsv" not in dead_names

    def test_tornado_request_handler_methods_not_dead(self):
        """Methods of tornado RequestHandler subclasses are framework hooks."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="HelloHandler", file_path="/repo/handlers.py",
            line_start=1, line_end=20, language="python",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="INHERITS", source="/repo/handlers.py::HelloHandler",
            target="tornado.web.RequestHandler", file_path="/repo/handlers.py", line=1,
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="get", file_path="/repo/handlers.py",
            line_start=3, line_end=5, language="python", parent_name="HelloHandler",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "get" not in dead_names

    def test_enum_class_members_not_dead(self):
        """Enum classes and their members are not flagged as dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="Color", file_path="/repo/enums.py",
            line_start=1, line_end=10, language="python",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="INHERITS", source="/repo/enums.py::Color",
            target="enum.Enum", file_path="/repo/enums.py", line=1,
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="value", file_path="/repo/enums.py",
            line_start=2, line_end=3, language="python", parent_name="Color",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "Color" not in dead_names
        assert "value" not in dead_names

    def test_namespace_class_not_dead(self):
        """Pure namespace classes (only nested types) are not dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="Outer", file_path="/repo/ns.py",
            line_start=1, line_end=20, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Class", name="Inner", file_path="/repo/ns.py",
            line_start=2, line_end=5, language="python", parent_name="Outer",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Type", name="Alias", file_path="/repo/ns.py",
            line_start=7, line_end=7, language="python", parent_name="Outer",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "Outer" not in dead_names

    def test_dispatch_handler_not_dead(self):
        """Convention-private dispatch/handler helpers are not dead code."""
        for name in ("_handle_event", "_format_message", "_process_item",
                     "_parse_payload", "_dispatch_request"):
            self.store.upsert_node(NodeInfo(
                kind="Function", name=name, file_path="/repo/handlers.py",
                line_start=1, line_end=2, language="python",
            ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        for name in ("_handle_event", "_format_message", "_process_item",
                     "_parse_payload", "_dispatch_request"):
            assert name not in dead_names, f"{name} should not be flagged dead"

    def test_public_format_function_can_be_dead(self):
        """Public ``format_*`` names remain eligible for dead-code detection."""
        self.store.upsert_node(NodeInfo(
            kind="Function", name="format_date", file_path="/repo/utils.py",
            line_start=1, line_end=2, language="python",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "format_date" in dead_names

    def test_dataclass_method_not_dead(self):
        """Methods of @dataclass-decorated classes are not dead code."""
        self.store.upsert_node(NodeInfo(
            kind="Class", name="User", file_path="/repo/models.py",
            line_start=1, line_end=20, language="python",
            extra={"decorators": ["dataclass"]},
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="full_name", file_path="/repo/models.py",
            line_start=3, line_end=5, language="python", parent_name="User",
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "User" not in dead_names
        assert "full_name" not in dead_names


class TestSuggestRefactorings:
    """Tests for suggest_refactorings."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed(self):
        """Seed with dead code to generate suggestions."""
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/lib.py", file_path="/repo/lib.py",
            line_start=1, line_end=50, language="python",
        ))
        # Unreferenced function -> removal suggestion
        self.store.upsert_node(NodeInfo(
            kind="Function", name="orphan_func", file_path="/repo/lib.py",
            line_start=10, line_end=20, language="python",
        ))
        self.store.commit()

    def test_suggest_refactorings(self):
        """suggest_refactorings returns a list of suggestions."""
        suggestions = suggest_refactorings(self.store)
        assert isinstance(suggestions, list)
        # Should have at least the dead-code removal suggestion
        assert len(suggestions) >= 1
        types = {s["type"] for s in suggestions}
        assert "remove" in types

    def test_suggestion_structure(self):
        """Each suggestion has the required fields."""
        suggestions = suggest_refactorings(self.store)
        for s in suggestions:
            assert "type" in s
            assert "description" in s
            assert "symbols" in s
            assert "rationale" in s
            assert s["type"] in ("move", "remove")


class TestApplyRefactor:
    """Tests for apply_refactor."""

    def setup_method(self):
        with _refactor_lock:
            _pending_refactors.clear()

    def teardown_method(self):
        with _refactor_lock:
            _pending_refactors.clear()

    def test_apply_refactor_validates_id(self):
        """apply_refactor rejects nonexistent refactor_id."""
        # Use a real temp dir as repo_root (needs .git or .code-review-graph)
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        try:
            result = apply_refactor("nonexistent_id", tmp_dir)
            assert result["status"] == "error"
            assert "not found" in result["error"].lower() or "expired" in result["error"].lower()
        finally:
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()

    def test_apply_refactor_expiry(self):
        """apply_refactor rejects expired previews."""
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        try:
            # Insert a preview that is already expired.
            rid = "expired1"
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "type": "rename",
                    "old_name": "old",
                    "new_name": "new",
                    "edits": [],
                    "stats": {"high": 0, "medium": 0, "low": 0},
                    "created_at": time.time() - REFACTOR_EXPIRY_SECONDS - 10,
                }
            result = apply_refactor(rid, tmp_dir)
            assert result["status"] == "error"
            assert "expired" in result["error"].lower()
        finally:
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()

    def test_apply_refactor_path_traversal(self):
        """apply_refactor blocks edits outside repo root."""
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        try:
            rid = "traversal"
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "type": "rename",
                    "old_name": "old",
                    "new_name": "new",
                    "edits": [{
                        "file": "/etc/passwd",
                        "line": 1,
                        "old": "old",
                        "new": "new",
                        "confidence": "high",
                    }],
                    "stats": {"high": 1, "medium": 0, "low": 0},
                    "created_at": time.time(),
                }
            result = apply_refactor(rid, tmp_dir)
            assert result["status"] == "error"
            assert "outside repo root" in result["error"].lower()
        finally:
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()

    def test_apply_refactor_success(self):
        """apply_refactor applies string replacement to a real file."""
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        target_file = tmp_dir / "example.py"
        target_file.write_text("def old_func():\n    pass\n", encoding="utf-8")
        try:
            rid = "success1"
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "type": "rename",
                    "old_name": "old_func",
                    "new_name": "new_func",
                    "edits": [{
                        "file": str(target_file),
                        "line": 1,
                        "old": "old_func",
                        "new": "new_func",
                        "confidence": "high",
                    }],
                    "stats": {"high": 1, "medium": 0, "low": 0},
                    "created_at": time.time(),
                }
            result = apply_refactor(rid, tmp_dir)
            assert result["status"] == "ok"
            assert result["edits_applied"] == 1
            assert len(result["files_modified"]) == 1
            # Verify file content was changed.
            content = target_file.read_text(encoding="utf-8")
            assert "new_func" in content
            assert "old_func" not in content
        finally:
            target_file.unlink(missing_ok=True)
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()

    def test_apply_refactor_dry_run_returns_diff_without_writing(self):
        """dry_run=True returns a unified diff without touching disk and
        keeps the refactor_id valid for a follow-up write (#176)."""
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        target_file = tmp_dir / "example.py"
        original = "def old_func():\n    pass\n"
        target_file.write_text(original, encoding="utf-8")
        try:
            rid = "dryrun1"
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "type": "rename",
                    "old_name": "old_func",
                    "new_name": "new_func",
                    "edits": [{
                        "file": str(target_file),
                        "line": 1,
                        "old": "old_func",
                        "new": "new_func",
                        "confidence": "high",
                    }],
                    "stats": {"high": 1, "medium": 0, "low": 0},
                    "created_at": time.time(),
                }

            # Step 1: dry_run — no writes, returns diff
            result = apply_refactor(rid, tmp_dir, dry_run=True)
            assert result["status"] == "ok"
            assert result["dry_run"] is True
            assert result["edits_applied"] == 1
            assert len(result["would_modify"]) == 1
            assert result["files_modified"] == []  # nothing written yet
            assert str(target_file) in result["would_modify"]
            # Diff should mention both the old and new name
            diff = result["diffs"][str(target_file)]
            assert "-def old_func():" in diff
            assert "+def new_func():" in diff
            # File on disk must be unchanged
            assert target_file.read_text(encoding="utf-8") == original

            # Step 2: refactor_id should still be valid — dry_run doesn't consume it
            with _refactor_lock:
                assert rid in _pending_refactors

            # Step 3: real apply — uses same refactor_id
            real_result = apply_refactor(rid, tmp_dir, dry_run=False)
            assert real_result["status"] == "ok"
            assert real_result.get("dry_run") is None  # not set on the real path
            assert real_result["edits_applied"] == 1
            assert len(real_result["files_modified"]) == 1
            # File content changed
            new_content = target_file.read_text(encoding="utf-8")
            assert "new_func" in new_content
            assert "old_func" not in new_content

            # refactor_id consumed after real apply
            with _refactor_lock:
                assert rid not in _pending_refactors
        finally:
            target_file.unlink(missing_ok=True)
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()

    def test_apply_refactor_dry_run_no_edits(self):
        """dry_run with an empty edit list returns an empty diff dict."""
        tmp_dir = Path(tempfile.mkdtemp())
        (tmp_dir / ".git").mkdir()
        try:
            rid = "dryrun-empty"
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "type": "rename",
                    "old_name": "x",
                    "new_name": "y",
                    "edits": [],
                    "stats": {"high": 0, "medium": 0, "low": 0},
                    "created_at": time.time(),
                }
            result = apply_refactor(rid, tmp_dir, dry_run=True)
            assert result["status"] == "ok"
            assert result["dry_run"] is True
            assert result["would_modify"] == []
            assert result["diffs"] == {}
        finally:
            with _refactor_lock:
                _pending_refactors.pop("dryrun-empty", None)
            (tmp_dir / ".git").rmdir()
            tmp_dir.rmdir()


class TestPendingRefactorsThreadSafe:
    """Tests for thread-safety of the pending refactors storage."""

    def test_pending_refactors_thread_safe(self):
        """The _refactor_lock is a threading.Lock instance."""
        assert isinstance(_refactor_lock, type(threading.Lock()))

    def test_concurrent_access(self):
        """Multiple threads can safely access _pending_refactors."""
        results = []

        def writer(rid: str):
            with _refactor_lock:
                _pending_refactors[rid] = {
                    "refactor_id": rid,
                    "created_at": time.time(),
                }
                results.append(rid)

        threads = [threading.Thread(target=writer, args=(f"t{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with _refactor_lock:
            assert len(results) == 10
            assert len(_pending_refactors) >= 10
            # Clean up
            _pending_refactors.clear()


class TestFindDeadCodeWithReferences:
    """Tests for REFERENCES-aware dead code detection."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed(self):
        """Seed with functions that have REFERENCES edges (map dispatch pattern)."""
        # File
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/handlers.ts", file_path="/repo/handlers.ts",
            line_start=1, line_end=100, language="typescript",
        ))
        # A function referenced in a map (should NOT be dead)
        self.store.upsert_node(NodeInfo(
            kind="Function", name="handleCreate", file_path="/repo/handlers.ts",
            line_start=10, line_end=20, language="typescript",
        ))
        # A function with CALLS edge (should NOT be dead)
        self.store.upsert_node(NodeInfo(
            kind="Function", name="calledFunc", file_path="/repo/handlers.ts",
            line_start=30, line_end=40, language="typescript",
        ))
        # A truly dead function (no edges at all)
        self.store.upsert_node(NodeInfo(
            kind="Function", name="deadFunc", file_path="/repo/handlers.ts",
            line_start=50, line_end=60, language="typescript",
        ))
        # Caller
        self.store.upsert_node(NodeInfo(
            kind="Function", name="dispatch", file_path="/repo/handlers.ts",
            line_start=70, line_end=80, language="typescript",
        ))
        # REFERENCES edge: dispatch -> handleCreate (map dispatch pattern)
        self.store.upsert_edge(EdgeInfo(
            kind="REFERENCES", source="/repo/handlers.ts::dispatch",
            target="/repo/handlers.ts::handleCreate",
            file_path="/repo/handlers.ts", line=75,
        ))
        # CALLS edge: dispatch -> calledFunc
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/handlers.ts::dispatch",
            target="/repo/handlers.ts::calledFunc",
            file_path="/repo/handlers.ts", line=76,
        ))
        self.store.commit()

    def test_referenced_function_not_dead(self):
        """Functions with REFERENCES edges should NOT be flagged as dead code."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "handleCreate" not in dead_names

    def test_called_function_not_dead(self):
        """Functions with CALLS edges remain excluded (existing behavior)."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "calledFunc" not in dead_names

    def test_truly_dead_function_still_reported(self):
        """Functions with no edges at all should still be flagged as dead code."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "deadFunc" in dead_names

    def test_only_references_edge_sufficient(self):
        """A function with ONLY a REFERENCES edge (no CALLS/IMPORTS) is not dead."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        # handleCreate has only a REFERENCES edge, no CALLS targeting it
        assert "handleCreate" not in dead_names


class TestFindDeadCodeWithTestedBy:
    """Regression for #515: dead-code detection must read TESTED_BY edges
    in the canonical direction (source=production, target=test).

    A production function whose only reference is its test must NOT be
    flagged as dead. Before the fix, find_dead_code looked for TESTED_BY
    edges where the production node was the *target*, but the parser writes
    the production node as the *source*, so tested-but-uncalled functions
    were wrongly reported as dead.
    """

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)
        self._seed()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _seed(self):
        # Production file with a function that is tested but never called.
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/calc.py", file_path="/repo/calc.py",
            line_start=1, line_end=100, language="python",
        ))
        # Unconventional name so no naming-convention heuristic rescues it.
        self.store.upsert_node(NodeInfo(
            kind="Function", name="combine", file_path="/repo/calc.py",
            line_start=10, line_end=20, language="python",
        ))
        # A truly dead function (no edges at all).
        self.store.upsert_node(NodeInfo(
            kind="Function", name="orphan", file_path="/repo/calc.py",
            line_start=30, line_end=40, language="python",
        ))
        # Test file + test.
        self.store.upsert_node(NodeInfo(
            kind="File", name="/repo/spec.py", file_path="/repo/spec.py",
            line_start=1, line_end=50, language="python",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Test", name="verify_combine_behaviour",
            file_path="/repo/spec.py", line_start=5, line_end=10,
            language="python", is_test=True,
        ))
        # Canonical TESTED_BY: source=production, target=test.
        self.store.upsert_edge(EdgeInfo(
            kind="TESTED_BY",
            source="/repo/calc.py::combine",
            target="/repo/spec.py::verify_combine_behaviour",
            file_path="/repo/spec.py", line=6,
        ))
        self.store.commit()

    def test_tested_function_not_dead(self):
        """A function whose only reference is a canonical TESTED_BY edge
        (source=production) must not be flagged as dead."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "combine" not in dead_names

    def test_orphan_function_still_dead(self):
        """A function with no edges at all is still reported as dead."""
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "orphan" in dead_names


class TestTransitiveImportResolution:
    """Tests for 2-hop transitive import resolution in plausible caller."""

    def setup_method(self):
        self.store = GraphStore(":memory:")
        for f in ("/repo/consumer.ts", "/repo/lib/index.ts", "/repo/lib/utils.ts"):
            self.store.upsert_node(NodeInfo(
                kind="File", name=f, file_path=f,
                line_start=1, line_end=50, language="typescript",
            ))

    def test_transitive_import_via_barrel_file(self):
        """consumer.ts imports index.ts which re-exports from utils.ts.
        A bare-name CALLS from consumer.ts should be plausible for utils.ts functions."""
        # Function defined in utils.ts
        self.store.upsert_node(NodeInfo(
            kind="Function", name="safeJsonParse",
            file_path="/repo/lib/utils.ts",
            line_start=10, line_end=20, language="typescript",
        ))
        self.store.upsert_node(NodeInfo(
            kind="Function", name="processData",
            file_path="/repo/consumer.ts",
            line_start=1, line_end=8, language="typescript",
        ))
        # Import chain: consumer -> index -> utils
        self.store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM", source="/repo/consumer.ts",
            target="/repo/lib/index.ts", file_path="/repo/consumer.ts", line=1,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="IMPORTS_FROM", source="/repo/lib/index.ts",
            target="/repo/lib/utils.ts", file_path="/repo/lib/index.ts", line=1,
        ))
        # Bare-name CALLS from consumer
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source="/repo/consumer.ts::processData",
            target="safeJsonParse", file_path="/repo/consumer.ts", line=5,
        ))
        self.store.commit()
        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "safeJsonParse" not in dead_names, (
            "2-hop import chain should make consumer a plausible caller"
        )


class TestFindDeadCodeModuleScope:
    """End-to-end regression: parse → store → find_dead_code.

    Pins the contract that functions invoked only from module scope are not
    flagged as dead. Bypasses the hand-built graph fixtures used elsewhere in
    this file so that a regression in any of the parser's 5 module-scope
    CALLS paths is caught.
    """

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()  # release the handle before GraphStore reopens it on Windows
        self.store = GraphStore(self.tmp.name)
        self.parser = CodeParser()

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _store_parsed(self, path: Path, source: bytes) -> None:
        nodes, edges = self.parser.parse_bytes(path, source)
        for n in nodes:
            self.store.upsert_node(n)
        for e in edges:
            self.store.upsert_edge(e)
        self.store.commit()

    def test_module_scope_caller_prevents_dead_code_flag(self, tmp_path):
        """A function called only from top-level script glue is not dead."""
        # ``run_job`` has no non-dunder name match and no framework decorator,
        # so without the module-scope CALLS fix it would be flagged dead.
        path = tmp_path / "script.py"
        path.write_bytes(
            b"def run_job():\n"
            b"    return 1\n"
            b"\n"
            b"run_job()\n"
        )
        self._store_parsed(path, path.read_bytes())

        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "run_job" not in dead_names, (
            "module-scope caller should prevent run_job from being flagged dead"
        )

    def test_if_main_block_caller_prevents_dead_code_flag(self, tmp_path):
        """A function called only inside ``if __name__ == '__main__'`` is not dead."""
        path = tmp_path / "cli.py"
        path.write_bytes(
            b"def launch():\n"
            b"    return 1\n"
            b"\n"
            b"if __name__ == '__main__':\n"
            b"    launch()\n"
        )
        self._store_parsed(path, path.read_bytes())

        dead = find_dead_code(self.store)
        dead_names = {d["name"] for d in dead}
        assert "launch" not in dead_names


class TestFindDeadCodeMRO:
    """MRO / polymorphic-dispatch false positives (OO reachability)."""

    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = GraphStore(self.tmp.name)

    def teardown_method(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _cls(self, name, file_path, line=5):
        self.store.upsert_node(NodeInfo(
            kind="Class", name=name, file_path=file_path,
            line_start=line, line_end=line + 40, language="python",
        ))

    def _meth(self, cls, name, file_path, line):
        self.store.upsert_node(NodeInfo(
            kind="Function", name=name, file_path=file_path,
            line_start=line, line_end=line + 5, language="python",
            parent_name=cls,
        ))

    def _inherits(self, child_qn, base_name, file_path, line):
        self.store.upsert_edge(EdgeInfo(
            kind="INHERITS", source=child_qn, target=base_name,
            file_path=file_path, line=line,
        ))

    def _caller(self, name, file_path, line):
        self.store.upsert_node(NodeInfo(
            kind="Function", name=name, file_path=file_path,
            line_start=line, line_end=line + 3, language="python",
        ))

    def _calls(self, src, tgt, file_path, line):
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source=src, target=tgt,
            file_path=file_path, line=line,
        ))

    def test_base_method_alive_when_only_override_called(self):
        f = "/repo/handlers.py"
        self._cls("Base", f, 5)
        self._cls("Sub", f, 50)
        self._meth("Base", "process", f, 10)
        self._meth("Sub", "process", f, 55)
        self._inherits(f + "::Sub", "Base", f, 50)
        self._caller("run", f, 90)
        self._calls(f + "::run", f + "::Sub.process", f, 92)
        self.store.commit()
        dead_q = {d["qualified_name"] for d in find_dead_code(self.store)}
        assert f + "::Base.process" not in dead_q

    def test_override_alive_when_base_called(self):
        f = "/repo/handlers.py"
        self._cls("Base", f, 5)
        self._cls("Sub", f, 50)
        self._meth("Base", "process", f, 10)
        self._meth("Sub", "process", f, 55)
        self._inherits(f + "::Sub", "Base", f, 50)
        self._caller("run", f, 90)
        self._calls(f + "::run", f + "::Base.process", f, 92)
        self.store.commit()
        dead_q = {d["qualified_name"] for d in find_dead_code(self.store)}
        assert f + "::Sub.process" not in dead_q

    def test_transitive_mro_three_levels(self):
        f = "/repo/chain.py"
        self._cls("A", f, 5)
        self._cls("B", f, 50)
        self._cls("C", f, 95)
        self._meth("A", "run", f, 10)
        self._meth("C", "run", f, 100)
        self._inherits(f + "::B", "A", f, 50)
        self._inherits(f + "::C", "B", f, 95)
        self._caller("main", f, 140)
        self._calls(f + "::main", f + "::A.run", f, 142)
        self.store.commit()
        dead_q = {d["qualified_name"] for d in find_dead_code(self.store)}
        assert f + "::C.run" not in dead_q

    def test_cross_file_inheritance_override_alive(self):
        base_f = "/repo/pkg/base.py"
        sub_f = "/repo/pkg/impl.py"
        self._cls("BaseHandler", base_f, 5)
        self._cls("JsonHandler", sub_f, 5)
        self._meth("BaseHandler", "handle", base_f, 10)
        self._meth("JsonHandler", "handle", sub_f, 10)
        self._inherits(sub_f + "::JsonHandler", "BaseHandler", sub_f, 5)
        self._caller("dispatch", base_f, 40)
        self._calls(base_f + "::dispatch", base_f + "::BaseHandler.handle", base_f, 42)
        self.store.commit()
        dead_q = {d["qualified_name"] for d in find_dead_code(self.store)}
        assert sub_f + "::JsonHandler.handle" not in dead_q

    def test_unrelated_class_same_method_name_still_dead(self):
        f = "/repo/two.py"
        self._cls("Alpha", f, 5)
        self._cls("Beta", f, 50)
        self._meth("Alpha", "save", f, 10)
        self._meth("Beta", "save", f, 55)
        self._caller("go", f, 90)
        self._calls(f + "::go", f + "::Alpha.save", f, 92)
        self.store.commit()
        dead_q = {d["qualified_name"] for d in find_dead_code(self.store)}
        assert f + "::Beta.save" in dead_q
        assert f + "::Alpha.save" not in dead_q

    def test_call_only_from_dead_guard_still_dead(self):
        """A function whose ONLY caller edge is tagged reachable=False
        (call sits inside `if False:` / `if TYPE_CHECKING:`) is still dead.
        Completes PR #580: parser tags the flag, find_dead_code consumes it."""
        f = "/repo/guard.py"
        self._caller("caller", f, 5)
        self.store.upsert_node(NodeInfo(
            kind="Function", name="only_in_guard", file_path=f,
            line_start=20, line_end=25, language="python",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f + "::caller",
            target=f + "::only_in_guard", file_path=f, line=8,
            extra={"reachable": False},
        ))
        self.store.commit()
        dead_q = {d["qualified_name"] for d in find_dead_code(self.store)}
        assert f + "::only_in_guard" in dead_q

    def test_live_and_dead_guard_call_keeps_alive(self):
        """If a function has a live call AND a dead-guard call, it stays alive."""
        f = "/repo/guard2.py"
        self._caller("live_caller", f, 5)
        self._caller("guard_caller", f, 30)
        self.store.upsert_node(NodeInfo(
            kind="Function", name="target", file_path=f,
            line_start=50, line_end=55, language="python",
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f + "::live_caller",
            target=f + "::target", file_path=f, line=8,
        ))
        self.store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f + "::guard_caller",
            target=f + "::target", file_path=f, line=33,
            extra={"reachable": False},
        ))
        self.store.commit()
        dead_q = {d["qualified_name"] for d in find_dead_code(self.store)}
        assert f + "::target" not in dead_q


class TestPythonEnrichmentWiring:
    """Jedi Python call-resolution pass must be wired into the build."""

    def test_full_build_reports_python_enrichment(self, tmp_path):
        from code_review_graph.incremental import full_build
        (tmp_path / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
        store = GraphStore(str(tmp_path / "graph.db"))
        try:
            result = full_build(tmp_path, store)
        finally:
            store.close()
        assert "python_enrichment" in result

    def test_incremental_reports_python_enrichment_when_py_changed(self, tmp_path):
        from code_review_graph.incremental import full_build, incremental_update
        (tmp_path / "a.py").write_text("def foo():\n    return 1\n", encoding="utf-8")
        store = GraphStore(str(tmp_path / "graph.db"))
        try:
            full_build(tmp_path, store)
            result = incremental_update(tmp_path, store, changed_files=["a.py"])
        finally:
            store.close()
        assert "python_enrichment" in result


class TestFindDeadCodePerformance:
    """Performance benchmark for dead-code detection on synthetic graphs."""

    def _build_synthetic_store(self, n_functions: int = 500) -> tuple[GraphStore, str]:
        """Create a store with *n_functions* functions and a sparse call graph."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        store = GraphStore(tmp.name)
        # Seed file node
        store.upsert_node(NodeInfo(
            kind="File", name="/repo/app.py", file_path="/repo/app.py",
            line_start=1, line_end=n_functions * 10, language="python",
        ))
        # Create functions
        for i in range(n_functions):
            store.upsert_node(NodeInfo(
                kind="Function", name=f"func_{i}", file_path="/repo/app.py",
                line_start=i * 10 + 1, line_end=i * 10 + 5, language="python",
            ))
        # Wire a sparse call graph: each function calls the next one.
        for i in range(0, n_functions - 1, 2):
            store.upsert_edge(EdgeInfo(
                kind="CALLS",
                source=f"/repo/app.py::func_{i}",
                target=f"/repo/app.py::func_{i + 1}",
                file_path="/repo/app.py", line=i * 10 + 2,
            ))
        store.commit()
        return store, tmp.name

    def test_find_dead_code_performance(self, benchmark):
        """Benchmark find_dead_code on a moderately large synthetic graph."""
        store, db_path = self._build_synthetic_store(n_functions=500)
        try:
            result = benchmark(find_dead_code, store)
            # Sanity: about half the functions have no incoming calls.
            assert len(result) > 100
        finally:
            store.close()
            Path(db_path).unlink(missing_ok=True)
