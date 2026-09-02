"""Shared pytest fixtures and fallbacks."""

import pytest

try:
    import pytest_benchmark  # noqa: F401
    _HAS_PYTEST_BENCHMARK = True
except ImportError:
    _HAS_PYTEST_BENCHMARK = False


if not _HAS_PYTEST_BENCHMARK:
    @pytest.fixture
    def benchmark():
        """Fallback benchmark fixture when pytest-benchmark is not installed."""

        class _Benchmark:
            def __call__(self, func, *args, **kwargs):
                return func(*args, **kwargs)

        return _Benchmark()
    """Autouse and unconditional: an opt-in fixture would silently stop
    protecting a test the day someone forgets to request it.
    """
    home = tmp_path_factory.mktemp("crg-home")
    monkeypatch.setenv("CRG_HOME", str(home))
    # The Hermes Agent installer resolves its config from ``HERMES_HOME``,
    # falling back to ``~/.hermes``. That fallback reaches the real user
    # config in any test that does not also patch ``Path.home()``, so pin
    # the variable to a temp directory instead of merely clearing it:
    # unset, a miss would be silently destructive; set, it cannot be.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path_factory.mktemp("hermes-home")))
    return home
