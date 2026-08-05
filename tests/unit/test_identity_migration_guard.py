"""Identity-migration maintenance guard: marker lifecycle, fail-closed
semantics, and entry-point guards for production writers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ingest.locking import (
    assert_no_active_identity_migration,
    create_identity_migration_marker,
    identity_migration_marker_path,
    read_identity_migration_marker,
    remove_identity_migration_marker,
)

RUN_ID = "pdf_identity_test_001"
PLAN_HASH = "a" * 64


class TestMarkerLifecycle:
    def test_absent_marker_passes(self, tmp_path: Path) -> None:
        assert_no_active_identity_migration(tmp_path)
        assert read_identity_migration_marker(tmp_path) is None

    def test_create_read_remove_roundtrip(self, tmp_path: Path) -> None:
        marker = create_identity_migration_marker(
            tmp_path,
            {
                "schema_version": "1.0",
                "run_id": RUN_ID,
                "plan_content_hash": PLAN_HASH,
                "journal_path": "data/transactions/pdf_identity/x/journal.json",
                "phase": "receipts_applying",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
        )
        assert marker.is_file()
        value = read_identity_migration_marker(tmp_path)
        assert value["run_id"] == RUN_ID
        assert value["plan_content_hash"] == PLAN_HASH
        assert identity_migration_marker_path(tmp_path) == marker

    def test_second_active_migration_fails_closed(self, tmp_path: Path) -> None:
        create_identity_migration_marker(tmp_path, {"run_id": RUN_ID})
        with pytest.raises(RuntimeError, match="already active"):
            create_identity_migration_marker(tmp_path, {"run_id": "other"})

    def test_guard_raises_while_active(self, tmp_path: Path) -> None:
        create_identity_migration_marker(tmp_path, {"run_id": RUN_ID})
        with pytest.raises(RuntimeError, match=RUN_ID):
            assert_no_active_identity_migration(tmp_path)

    def test_remove_requires_matching_identity(self, tmp_path: Path) -> None:
        create_identity_migration_marker(tmp_path, {"run_id": RUN_ID, "plan_content_hash": PLAN_HASH})
        with pytest.raises(RuntimeError, match="mismatch"):
            remove_identity_migration_marker(
                tmp_path, run_id="other", plan_content_hash=PLAN_HASH
            )
        with pytest.raises(RuntimeError, match="mismatch"):
            remove_identity_migration_marker(
                tmp_path, run_id=RUN_ID, plan_content_hash="b" * 64
            )
        remove_identity_migration_marker(
            tmp_path, run_id=RUN_ID, plan_content_hash=PLAN_HASH
        )
        assert not identity_migration_marker_path(tmp_path).exists()

    def test_corrupt_marker_is_fail_closed(self, tmp_path: Path) -> None:
        marker = identity_migration_marker_path(tmp_path)
        marker.write_text("{not json", encoding="utf-8")
        with pytest.raises(RuntimeError):
            assert_no_active_identity_migration(tmp_path)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        name.removesuffix(".py"), REPO_ROOT / "scripts" / name
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestEntryPointGuards:
    """Entry-point guards fail closed while the maintenance marker exists."""

    @pytest.mark.parametrize(
        "script,args",
        [
            ("fetch_pdf_for_paper_raw.py", ["--paper-number", "0000000000000001"]),
            ("resolve_paper_raw_metadata.py", ["--paper-number", "0000000000000001"]),
            ("convert_paper_raw_batch.py", ["--paper-number", "0000000000000001"]),
            ("freeze_paper_raw_metadata.py", ["--paper-number", "0000000000000001"]),
            ("confirm_paper_raw_pdf_identity.py",
             ["--paper-number", "0000000000000001", "--confirmed-by", "t", "--reason", "r"]),
            ("formalize_paper_raw.py", ["--paper-number", "0000000000000001"]),
            ("commit_paper_raw_to_papers.py", ["--paper-number", "0000000000000001"]),
        ],
    )
    def test_scripts_reject_while_marker_active(
        self, tmp_path: Path, script: str, args: list[str], capsys
    ) -> None:
        import sys

        create_identity_migration_marker(tmp_path, {"run_id": RUN_ID})
        module = _load_script(script)
        saved = sys.argv
        sys.argv = [script, *args, "--paper-raw-dir", str(tmp_path)]
        message = ""
        try:
            module.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:
            # The guard raises RuntimeError; under a real CLI that is an
            # uncaught traceback and a non-zero exit — fail closed.
            code = 1
            message = str(exc)
        else:
            code = 0
        finally:
            sys.argv = saved
        # Fail closed: non-zero exit and the marker is still present.
        assert code != 0
        assert identity_migration_marker_path(tmp_path).exists()
        combined = message + capsys.readouterr().out + capsys.readouterr().err
        assert "identity migration in progress" in combined

    def test_scripts_pass_without_marker(self, tmp_path: Path) -> None:
        import sys

        # No marker: the dry-run/plan path must NOT raise the guard.
        module = _load_script("freeze_paper_raw_metadata.py")
        saved = sys.argv
        sys.argv = ["freeze_paper_raw_metadata.py", "--paper-number",
                    "0000000000000001", "--paper-raw-dir", str(tmp_path)]
        try:
            module.main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        else:
            code = 0
        finally:
            sys.argv = saved
        assert code == 0
