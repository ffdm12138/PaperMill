from pathlib import Path

import pytest


pytestmark = pytest.mark.hygiene


def test_concurrent_discovery_entrypoint_has_no_subprocess_legacy():
    text = Path("scripts/discover_papers_concurrent.py").read_text(encoding="utf-8")
    forbidden = [
        "import subprocess",
        "Popen",
        "DISCOVER_SCRIPT",
        "ThreadPoolExecutor",
        "_build_command",
        "_run_one",
    ]
    for token in forbidden:
        assert token not in text


def test_discovery_code_does_not_import_script_validators():
    for path in Path("src/discovery").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "scripts.validate_v2_library" not in text
        assert "from scripts" not in text


def test_discovery_process_tests_do_not_use_queue_empty():
    paths = [
        Path("tests/contract/test_discovery_receipt_toctou.py"),
        Path("tests/integration/test_discovery_cross_process_locking.py"),
    ]
    for path in paths:
        assert ".empty()" not in path.read_text(encoding="utf-8")
