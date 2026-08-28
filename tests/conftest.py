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
