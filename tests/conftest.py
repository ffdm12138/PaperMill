"""Repository-wide pytest path and marker policy."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_LAYER_MARKERS = {
    "contract": ("contract",), "unit": ("unit",),
    "integration": ("integration",), "security": ("security",),
    "hygiene": ("hygiene",), "e2e": ("e2e",),
    "process": ("process", "slow"), "slow": ("slow",),
    "stress": ("stress", "slow"),
    # performance benchmarks are slow by construction (bulk fixtures); the
    # slow marker routes them into the full gate's sequential residue so
    # their I/O counters never share cores with xdist workers.
    "performance": ("performance", "slow"),
}
_PROCESS_MODULES = {
    "tests/unit/test_acceptance_runner.py",
    "tests/unit/test_acceptance_zombie.py",
    "tests/integration/test_discovery_cross_process_locking.py",
    "tests/hygiene/test_git_hygiene_real_repository.py",
    "tests/e2e/test_transaction_concurrency.py",
    "tests/e2e/test_sentinel_regression.py",
}
_SLOW_MODULES = {
    "tests/unit/test_discovery_dual_lane_scheduler.py",
    "tests/hygiene/test_no_hardcoded_openalex_secrets.py",
    "tests/hygiene/test_no_tombstone_tests.py",
    "tests/integration/test_paper_raw_convert_idempotency.py",
}


def pytest_collection_modifyitems(config, items):
    """Apply directory markers and explicit behavioral cost markers."""
    root = Path(str(config.rootpath))
    for item in items:
        rel = Path(str(item.path)).resolve().relative_to(root).as_posix()
        parts = Path(rel).parts
        if len(parts) >= 2 and parts[0] == "tests":
            for marker in _LAYER_MARKERS.get(parts[1], ()):
                item.add_marker(getattr(pytest.mark, marker))
        if rel in _PROCESS_MODULES:
            item.add_marker(pytest.mark.process)
            item.add_marker(pytest.mark.slow)
        if rel in _SLOW_MODULES:
            item.add_marker(pytest.mark.slow)
        if "conflict_loop_100" in item.name:
            item.add_marker(pytest.mark.stress)
            item.add_marker(pytest.mark.slow)
        if rel.endswith("test_discovery_cross_process_locking.py") and "allocator_new_allocation" in item.name:
            item.add_marker(pytest.mark.stress)
