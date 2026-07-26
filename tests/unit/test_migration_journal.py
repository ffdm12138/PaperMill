"""Unit tests for the Discovery v4 migration journal state machine."""
from __future__ import annotations

import pytest

from src.migrations.discovery_v4.migration_journal import (
    MigrationJournal,
    MigrationState,
    VALID_TRANSITIONS,
)


class TestMigrationStateTransitions:
    """Adjacent-only transitions with ABORTED and SMOKE_FAILED branches."""

    def test_adjacent_happy_path_transitions_are_allowed(self, tmp_path, monkeypatch):
        import config.settings as settings
        monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", tmp_path)

        happy_path = [
            MigrationState.PLANNED,
            MigrationState.INVENTORY_COMPLETE,
            MigrationState.ARCHIVE_PREPARED,
            MigrationState.WORKSPACE_BUILT,
            MigrationState.NOTEBOOKS_STAGED,
            MigrationState.CANDIDATES_EXTRACTED,
            MigrationState.PREFLIGHT_VALIDATED,
            MigrationState.SMOKE_PASSED,
            MigrationState.CUTOVER_COMMITTED,
            MigrationState.LEGACY_CLEANED,
            MigrationState.FINALIZED,
        ]
        journal = MigrationJournal.create("adjacent-test")
        for next_state in happy_path[1:]:
            journal.transition_to(next_state)
        assert journal.state == MigrationState.FINALIZED

    def test_non_adjacent_forward_transitions_are_rejected(self, tmp_path, monkeypatch):
        import config.settings as settings
        monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", tmp_path)

        journal = MigrationJournal.create("non-adjacent-test")
        with pytest.raises(ValueError, match="cannot transition"):
            journal.transition_to(MigrationState.CUTOVER_COMMITTED)

    def test_aborted_from_any_pre_cutover_state(self, tmp_path, monkeypatch):
        import config.settings as settings
        monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", tmp_path)

        pre_cutover = [
            MigrationState.PLANNED,
            MigrationState.INVENTORY_COMPLETE,
            MigrationState.ARCHIVE_PREPARED,
            MigrationState.WORKSPACE_BUILT,
            MigrationState.NOTEBOOKS_STAGED,
            MigrationState.CANDIDATES_EXTRACTED,
            MigrationState.PREFLIGHT_VALIDATED,
            MigrationState.SMOKE_FAILED,
            MigrationState.SMOKE_PASSED,
        ]
        for state in pre_cutover:
            journal = MigrationJournal.create(f"abort-{state.value}")
            if state != MigrationState.PLANNED:
                # Reach the target state through the adjacent happy path.
                path = [
                    MigrationState.PLANNED,
                    MigrationState.INVENTORY_COMPLETE,
                    MigrationState.ARCHIVE_PREPARED,
                    MigrationState.WORKSPACE_BUILT,
                    MigrationState.NOTEBOOKS_STAGED,
                    MigrationState.CANDIDATES_EXTRACTED,
                    MigrationState.PREFLIGHT_VALIDATED,
                    MigrationState.SMOKE_PASSED,
                ]
                for step in path[1:]:
                    journal.transition_to(step)
                    if journal.state == state:
                        break
            journal.transition_to(MigrationState.ABORTED)
            assert journal.state == MigrationState.ABORTED

    def test_aborted_from_cutover_committed_is_allowed_for_rollback(
        self, tmp_path, monkeypatch
    ):
        import config.settings as settings
        monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", tmp_path)

        journal = MigrationJournal.create("rollback-abort-test")
        path = [
            MigrationState.PLANNED,
            MigrationState.INVENTORY_COMPLETE,
            MigrationState.ARCHIVE_PREPARED,
            MigrationState.WORKSPACE_BUILT,
            MigrationState.NOTEBOOKS_STAGED,
            MigrationState.CANDIDATES_EXTRACTED,
            MigrationState.PREFLIGHT_VALIDATED,
            MigrationState.SMOKE_PASSED,
            MigrationState.CUTOVER_COMMITTED,
        ]
        for step in path[1:]:
            journal.transition_to(step)
        journal.transition_to(MigrationState.ABORTED)
        assert journal.state == MigrationState.ABORTED

    def test_aborted_from_post_cutover_states_is_rejected(self, tmp_path, monkeypatch):
        import config.settings as settings
        monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", tmp_path)

        # LEGACY_CLEANED / FINALIZED / ABORTED never abort; CUTOVER_COMMITTED
        # may still abort via the rollback path (covered above).
        post_cutover = [
            MigrationState.LEGACY_CLEANED,
            MigrationState.FINALIZED,
            MigrationState.ABORTED,
        ]
        for state in post_cutover:
            journal = MigrationJournal.create(f"no-abort-{state.value}")
            if state != MigrationState.PLANNED:
                path = [
                    MigrationState.PLANNED,
                    MigrationState.INVENTORY_COMPLETE,
                    MigrationState.ARCHIVE_PREPARED,
                    MigrationState.WORKSPACE_BUILT,
                    MigrationState.NOTEBOOKS_STAGED,
                    MigrationState.CANDIDATES_EXTRACTED,
                    MigrationState.PREFLIGHT_VALIDATED,
                    MigrationState.SMOKE_PASSED,
                    MigrationState.CUTOVER_COMMITTED,
                    MigrationState.LEGACY_CLEANED,
                    MigrationState.FINALIZED,
                ]
                for step in path[1:]:
                    journal.transition_to(step)
                    if journal.state == state:
                        break
            assert MigrationState.ABORTED not in VALID_TRANSITIONS[state]

    def test_smoke_failed_from_preflight(self, tmp_path, monkeypatch):
        import config.settings as settings
        monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", tmp_path)

        journal = MigrationJournal.create("smoke-failed-test")
        path = [
            MigrationState.PLANNED,
            MigrationState.INVENTORY_COMPLETE,
            MigrationState.ARCHIVE_PREPARED,
            MigrationState.WORKSPACE_BUILT,
            MigrationState.NOTEBOOKS_STAGED,
            MigrationState.CANDIDATES_EXTRACTED,
            MigrationState.PREFLIGHT_VALIDATED,
        ]
        for step in path[1:]:
            journal.transition_to(step)
        journal.transition_to(MigrationState.SMOKE_FAILED)
        assert journal.state == MigrationState.SMOKE_FAILED

    def test_smoke_failed_to_smoke_passed_then_cutover(self, tmp_path, monkeypatch):
        import config.settings as settings
        monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", tmp_path)

        journal = MigrationJournal.create("smoke-retry-test")
        path = [
            MigrationState.PLANNED,
            MigrationState.INVENTORY_COMPLETE,
            MigrationState.ARCHIVE_PREPARED,
            MigrationState.WORKSPACE_BUILT,
            MigrationState.NOTEBOOKS_STAGED,
            MigrationState.CANDIDATES_EXTRACTED,
            MigrationState.PREFLIGHT_VALIDATED,
            MigrationState.SMOKE_FAILED,
        ]
        for step in path[1:]:
            journal.transition_to(step)
        journal.transition_to(MigrationState.SMOKE_PASSED)
        journal.transition_to(MigrationState.CUTOVER_COMMITTED)
        assert journal.state == MigrationState.CUTOVER_COMMITTED

    def test_smoke_failed_to_aborted(self, tmp_path, monkeypatch):
        import config.settings as settings
        monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", tmp_path)

        journal = MigrationJournal.create("smoke-abort-test")
        path = [
            MigrationState.PLANNED,
            MigrationState.INVENTORY_COMPLETE,
            MigrationState.ARCHIVE_PREPARED,
            MigrationState.WORKSPACE_BUILT,
            MigrationState.NOTEBOOKS_STAGED,
            MigrationState.CANDIDATES_EXTRACTED,
            MigrationState.PREFLIGHT_VALIDATED,
            MigrationState.SMOKE_FAILED,
        ]
        for step in path[1:]:
            journal.transition_to(step)
        journal.transition_to(MigrationState.ABORTED)
        assert journal.state == MigrationState.ABORTED

    def test_cannot_leave_aborted(self, tmp_path, monkeypatch):
        import config.settings as settings
        monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", tmp_path)

        journal = MigrationJournal.create("aborted-terminal-test")
        journal.transition_to(MigrationState.ABORTED)
        with pytest.raises(ValueError, match="cannot transition"):
            journal.transition_to(MigrationState.INVENTORY_COMPLETE)

    def test_smoke_failed_to_any_other_than_smoke_passed_or_aborted_is_rejected(
        self, tmp_path, monkeypatch
    ):
        import config.settings as settings
        monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", tmp_path)

        journal = MigrationJournal.create("smoke-failed-reject-test")
        path = [
            MigrationState.PLANNED,
            MigrationState.INVENTORY_COMPLETE,
            MigrationState.ARCHIVE_PREPARED,
            MigrationState.WORKSPACE_BUILT,
            MigrationState.NOTEBOOKS_STAGED,
            MigrationState.CANDIDATES_EXTRACTED,
            MigrationState.PREFLIGHT_VALIDATED,
            MigrationState.SMOKE_FAILED,
        ]
        for step in path[1:]:
            journal.transition_to(step)
        with pytest.raises(ValueError, match="cannot transition"):
            journal.transition_to(MigrationState.FINALIZED)

    def test_preflight_can_still_go_directly_to_smoke_passed(self, tmp_path, monkeypatch):
        import config.settings as settings
        monkeypatch.setattr(settings, "DISCOVERY_MIGRATIONS_DIR", tmp_path)

        journal = MigrationJournal.create("smoke-passed-test")
        path = [
            MigrationState.PLANNED,
            MigrationState.INVENTORY_COMPLETE,
            MigrationState.ARCHIVE_PREPARED,
            MigrationState.WORKSPACE_BUILT,
            MigrationState.NOTEBOOKS_STAGED,
            MigrationState.CANDIDATES_EXTRACTED,
            MigrationState.PREFLIGHT_VALIDATED,
        ]
        for step in path[1:]:
            journal.transition_to(step)
        journal.transition_to(MigrationState.SMOKE_PASSED)
        assert journal.state == MigrationState.SMOKE_PASSED
