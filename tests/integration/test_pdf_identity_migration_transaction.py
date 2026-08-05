"""Transactional PDF-identity migration: plan -> receipts-only ->
freeze-eligible, journaled substates, fail-closed phases, abort restore."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

from src.metadata.freeze import freeze_metadata
from src.metadata.pdf_identity import extract_pdf_identity_evidence
from src.metadata.pdf_match import build_match_receipt, write_match_receipt
from src.metadata.schema import empty_metadata
from src.utils.file_fingerprint import compute_sha256

_REPO_ROOT = Path(__file__).resolve().parents[2]
PN_A = "0000000000000101"   # matched, freeze-eligible
PN_B = "0000000000000102"   # ambiguous (no freeze)
PN_C = "0000000000000103"   # legacy metadata state "mismatch"
PN_BAD = "0000000000000104"  # corrupt metadata (plan error)
DOI_A = "10.5194/acp-26-9643-2026"
DOI_B = "10.1007/s10546-021-00629"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "rematch_paper_raw_pdf_identity",
        _REPO_ROOT / "scripts" / "rematch_paper_raw_pdf_identity.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fitz_pdf(path: Path, doi: str, title: str = "Migration Paper",
              author: str = "Jane Smith") -> None:
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), title)
    page.insert_text((72, 100), f"doi:{doi}")
    page.insert_text((72, 114), author)
    doc.save(str(path), deflate=True)
    doc.close()


def _workspace(root: Path, paper_number: str, doi: str) -> Path:
    folder = root / paper_number
    folder.mkdir(parents=True)
    metadata = empty_metadata(paper_number)
    metadata["title"]["original"] = "Migration Paper"
    metadata["year"] = 2024
    metadata["authors"] = [{"full_name": "Jane Smith", "family": "Smith", "given": "Jane", "orcid": "", "affiliation": ""}]
    metadata["first_author"] = {"family": "Smith", "display": "Jane Smith"}
    metadata["container"]["journal"] = "Test Journal"
    metadata["identifiers"]["doi"] = doi
    metadata["source"].update({"provider": "fixture", "raw_record_path": "source_records/metadata_source.fixture.json"})
    (folder / f"{paper_number}.metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / f"{paper_number}.paper.number").write_text(
        json.dumps({"paper_number": paper_number, "folder_name": paper_number, "state": "active"}),
        encoding="utf-8",
    )
    (folder / "source_records").mkdir()
    (folder / metadata["source"]["raw_record_path"]).write_text(json.dumps({"doi": doi}), encoding="utf-8")
    return folder


def _run(module, *argv: str, capsys=None) -> tuple[int, str]:
    saved = sys.argv
    sys.argv = ["rematch_paper_raw_pdf_identity.py", *argv]
    try:
        code = module.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        code = 1
        if capsys is not None:
            capsys.readouterr()
        return code, str(exc)
    finally:
        sys.argv = saved
    output = (capsys.readouterr().out + capsys.readouterr().err) if capsys else ""
    return code, output


def _plan_hash(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("[PLAN-HASH] "):
            return line.split(" ", 1)[1].strip()
    raise AssertionError(f"no [PLAN-HASH] in output: {output[:500]}")


class TestFullMigration:
    def test_plan_receipts_freeze_happy_path(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "paper_raw"
        folder_a = _workspace(root, PN_A, DOI_A)
        _fitz_pdf(folder_a / f"{PN_A}.pdf", DOI_A)
        folder_b = _workspace(root, PN_B, DOI_B)
        # No bibliographic corroboration: different title, different author
        # (and no year on the page) -> the labeled first-page DOI stays
        # medium and the decision is ambiguous.
        _fitz_pdf(folder_b / f"{PN_B}.pdf", DOI_B, title="A Different Migration Paper",
                  author="Zhang Wei")

        module = _load_script()
        transaction_root = tmp_path / "transactions" / "pdf_identity"
        plan_file = tmp_path / "plan.json"
        code, output = _run(
            module, "--plan", "--all", "--paper-raw-dir", str(root),
            "--transaction-root", str(transaction_root), "--plan-file", str(plan_file),
            capsys=capsys,
        )
        assert code == 0
        plan_hash = _plan_hash(output)
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        # plan_file_sha256 must NOT be embedded in the hashed plan itself.
        assert "plan_file_sha256" not in plan
        baseline = json.loads(plan_file.with_name("baseline.json").read_text(encoding="utf-8"))
        assert baseline["plan_file_sha256"]

        # Receipts phase.
        code, output = _run(
            module, "--receipts-only", "--plan-file", str(plan_file),
            "--expected-plan-hash", plan_hash,
            "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
            capsys=capsys,
        )
        assert code == 0, output
        receipt_a = json.loads((folder_a / f"{PN_A}.metadata_match.json").read_text(encoding="utf-8"))
        assert receipt_a["schema_version"] == "2.0"
        assert receipt_a["match_status"] == "matched"
        # The old freeze (none here) was invalidated; no freeze may exist
        # right after the receipts phase.
        assert not (folder_a / f"{PN_A}.metadata_freeze.json").exists()
        status_a = json.loads((folder_a / ".import_status.json").read_text(encoding="utf-8"))
        assert status_a["metadata"]["state"] == "matched"
        receipt_b = json.loads((folder_b / f"{PN_B}.metadata_match.json").read_text(encoding="utf-8"))
        assert receipt_b["match_status"] == "ambiguous"
        status_b = json.loads((folder_b / ".import_status.json").read_text(encoding="utf-8"))
        assert status_b["metadata"]["state"] == "ambiguous"

        # Freeze phase.
        code, output = _run(
            module, "--freeze-eligible", "--plan-file", str(plan_file),
            "--expected-plan-hash", plan_hash,
            "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
            capsys=capsys,
        )
        assert code == 0, output
        freeze_a = json.loads((folder_a / f"{PN_A}.metadata_freeze.json").read_text(encoding="utf-8"))
        assert freeze_a["revision"] == 1
        assert freeze_a["frozen_at"] == plan["papers"][PN_A]["target_frozen_at"]
        assert not (folder_b / f"{PN_B}.metadata_freeze.json").exists()
        status_a = json.loads((folder_a / ".import_status.json").read_text(encoding="utf-8"))
        assert status_a["metadata"]["state"] == "frozen"
        # Maintenance marker removed.
        assert not (root / ".pdf_identity_migration.json").exists()

    def test_legacy_mismatch_status_tolerated(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "paper_raw"
        folder = _workspace(root, PN_C, DOI_A)
        _fitz_pdf(folder / f"{PN_C}.pdf", DOI_A)
        # Pre-migration v2 status with the legacy "mismatch" state, which
        # the v2-only runtime rejects.
        (folder / ".import_status.json").write_text(json.dumps({
            "schema_version": "2.0",
            "paper_number": PN_C,
            "metadata": {"state": "mismatch", "revision": 0},
            "pdf": {"state": "attached"},
            "conversion": {"state": "complete"},
            "catalog": {"state": "missing"},
            "formalization": {"state": "pending"},
            "commit": {"state": "pending"},
            "updated_at": "2026-01-01T00:00:00+00:00",
        }), encoding="utf-8")

        module = _load_script()
        transaction_root = tmp_path / "transactions" / "pdf_identity"
        plan_file = tmp_path / "plan.json"
        code, output = _run(
            module, "--plan", "--all", "--paper-raw-dir", str(root),
            "--transaction-root", str(transaction_root), "--plan-file", str(plan_file),
            capsys=capsys,
        )
        assert code == 0
        plan_hash = _plan_hash(output)
        code, output = _run(
            module, "--receipts-only", "--plan-file", str(plan_file),
            "--expected-plan-hash", plan_hash,
            "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
            capsys=capsys,
        )
        assert code == 0, output
        status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
        assert status["metadata"]["state"] == "matched"

    def test_failed_paper_blocks_phase_but_others_processed(
        self, tmp_path: Path, capsys
    ) -> None:
        root = tmp_path / "paper_raw"
        folder_a = _workspace(root, PN_A, DOI_A)
        _fitz_pdf(folder_a / f"{PN_A}.pdf", DOI_A)
        folder_bad = _workspace(root, PN_BAD, DOI_A)
        _fitz_pdf(folder_bad / f"{PN_BAD}.pdf", DOI_A)
        # Corrupt metadata: the plan records a plan_error; apply fails
        # closed on it while the healthy paper still migrates.
        (folder_bad / f"{PN_BAD}.metadata.json").write_text("{broken", encoding="utf-8")

        module = _load_script()
        transaction_root = tmp_path / "transactions" / "pdf_identity"
        plan_file = tmp_path / "plan.json"
        code, output = _run(
            module, "--plan", "--all", "--paper-raw-dir", str(root),
            "--transaction-root", str(transaction_root), "--plan-file", str(plan_file),
            capsys=capsys,
        )
        assert code == 0
        plan_hash = _plan_hash(output)
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        assert plan["papers"][PN_BAD].get("plan_error")

        code, output = _run(
            module, "--receipts-only", "--plan-file", str(plan_file),
            "--expected-plan-hash", plan_hash,
            "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
            capsys=capsys,
        )
        # Fail closed: non-zero exit, marker stays, healthy paper migrated.
        assert code != 0
        receipt_a = json.loads((folder_a / f"{PN_A}.metadata_match.json").read_text(encoding="utf-8"))
        assert receipt_a["schema_version"] == "2.0"
        assert (root / ".pdf_identity_migration.json").exists()
        journal = json.loads(
            (transaction_root / f"pdf_identity_{plan_hash[:16]}" / "journal.json")
            .read_text(encoding="utf-8")
        )
        assert journal["papers"][PN_A]["status"] == "status_written"
        assert journal["papers"][PN_BAD]["status"] == "failed"

        # Resume after fixing the metadata: the failed paper is retried
        # from its substate and the phase completes.
        (folder_bad / f"{PN_BAD}.metadata.json").write_text(
            json.dumps({
                "schema_version": "2.0",
                "paper_number": PN_BAD,
                "paper_raw_id": PN_BAD,
                "title": {"original": "Migration Paper"},
                "year": 2024,
                "authors": [{"family": "Smith", "given": "Jane"}],
                "first_author": {"family": "Smith"},
                "container": {"journal": "Test Journal"},
                "identifiers": {"doi": DOI_A},
                "source": {"provider": "fixture", "raw_record_path": "source_records/metadata_source.fixture.json"},
            }), encoding="utf-8",
        )
        # The previous run left the marker; abort restores the tree and
        # unblocks.  Order matters: abort BEFORE re-planning, so the new
        # plan's workspace inventory matches the restored tree.
        journal_path1 = transaction_root / f"pdf_identity_{plan_hash[:16]}" / "journal.json"
        code, output = _run(
            module, "--abort", str(journal_path1),
            "--paper-raw-dir", str(root), capsys=capsys,
        )
        assert code == 0
        assert not (root / ".pdf_identity_migration.json").exists()
        journal1 = json.loads(journal_path1.read_text(encoding="utf-8"))
        assert journal1["phase"] == "aborted"

        # Re-plan is required (the old plan pinned a plan_error).
        code, output = _run(
            module, "--plan", "--all", "--paper-raw-dir", str(root),
            "--transaction-root", str(transaction_root), "--plan-file", str(plan_file),
            capsys=capsys,
        )
        assert code == 0
        plan_hash2 = _plan_hash(output)

        code, output = _run(
            module, "--receipts-only", "--plan-file", str(plan_file),
            "--expected-plan-hash", plan_hash2,
            "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
            capsys=capsys,
        )
        assert code == 0, output
        journal2 = json.loads(
            (transaction_root / f"pdf_identity_{plan_hash2[:16]}" / "journal.json")
            .read_text(encoding="utf-8")
        )
        assert journal2["phase"] == "receipts_applied"

    def test_idempotent_reapply_and_freeze_no_revision_bump(
        self, tmp_path: Path, capsys
    ) -> None:
        root = tmp_path / "paper_raw"
        folder_a = _workspace(root, PN_A, DOI_A)
        _fitz_pdf(folder_a / f"{PN_A}.pdf", DOI_A)
        module = _load_script()
        transaction_root = tmp_path / "transactions" / "pdf_identity"
        plan_file = tmp_path / "plan.json"
        _run(module, "--plan", "--all", "--paper-raw-dir", str(root),
             "--transaction-root", str(transaction_root), "--plan-file", str(plan_file),
             capsys=capsys)
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        plan_hash = plan["plan_content_hash"]
        assert _run(module, "--receipts-only", "--plan-file", str(plan_file),
                    "--expected-plan-hash", plan_hash,
                    "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
                    capsys=capsys)[0] == 0
        assert _run(module, "--freeze-eligible", "--plan-file", str(plan_file),
                    "--expected-plan-hash", plan_hash,
                    "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
                    capsys=capsys)[0] == 0
        freeze_path = folder_a / f"{PN_A}.metadata_freeze.json"
        before = freeze_path.read_bytes()
        # Re-running the same plan must be a no-op (journal terminal).
        assert _run(module, "--receipts-only", "--plan-file", str(plan_file),
                    "--expected-plan-hash", plan_hash,
                    "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
                    capsys=capsys)[0] == 0
        assert _run(module, "--freeze-eligible", "--plan-file", str(plan_file),
                    "--expected-plan-hash", plan_hash,
                    "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
                    capsys=capsys)[0] == 0
        assert freeze_path.read_bytes() == before

    def test_existing_frozen_workspace_rebuilt_revision_plus_one(
        self, tmp_path: Path, capsys
    ) -> None:
        root = tmp_path / "paper_raw"
        folder = _workspace(root, PN_A, DOI_A)
        _fitz_pdf(folder / f"{PN_A}.pdf", DOI_A)
        # Simulate the pre-migration state: freeze with a v2 receipt, then
        # downgrade the receipt to schema 1.0 (the freeze closure now
        # references a v1 receipt — exactly the pre-migration shape).
        metadata = json.loads((folder / f"{PN_A}.metadata.json").read_text(encoding="utf-8"))
        evidence = extract_pdf_identity_evidence(pdf_path=folder / f"{PN_A}.pdf")
        receipt = build_match_receipt(folder, PN_A, metadata, evidence)
        write_match_receipt(folder, receipt)
        frozen = freeze_metadata(folder, PN_A)
        assert frozen["revision"] == 1
        receipt["schema_version"] = "1.0"
        write_match_receipt(folder, receipt)
        old_freeze_sha = compute_sha256(folder / f"{PN_A}.metadata_freeze.json")

        module = _load_script()
        transaction_root = tmp_path / "transactions" / "pdf_identity"
        plan_file = tmp_path / "plan.json"
        assert _run(module, "--plan", "--all", "--paper-raw-dir", str(root),
                    "--transaction-root", str(transaction_root), "--plan-file", str(plan_file),
                    capsys=capsys)[0] == 0
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        plan_hash = plan["plan_content_hash"]
        assert plan["papers"][PN_A]["old_freeze_existed"] is True
        # The plan pins revision = old + 1.
        assert plan["papers"][PN_A]["target_revision"] == 2

        assert _run(module, "--receipts-only", "--plan-file", str(plan_file),
                    "--expected-plan-hash", plan_hash,
                    "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
                    capsys=capsys)[0] == 0
        # Old freeze moved OUT of the active path during receipts.
        assert not (folder / f"{PN_A}.metadata_freeze.json").exists()
        run_dir = transaction_root / f"pdf_identity_{plan_hash[:16]}"
        assert (run_dir / "invalidated_freeze" / f"{PN_A}.metadata_freeze.json").is_file()

        assert _run(module, "--freeze-eligible", "--plan-file", str(plan_file),
                    "--expected-plan-hash", plan_hash,
                    "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
                    capsys=capsys)[0] == 0
        rebuilt = json.loads((folder / f"{PN_A}.metadata_freeze.json").read_text(encoding="utf-8"))
        assert rebuilt["revision"] == 2
        assert rebuilt["frozen_at"] == plan["papers"][PN_A]["target_frozen_at"]
        assert compute_sha256(folder / f"{PN_A}.metadata_freeze.json") == \
            plan["papers"][PN_A]["target_freeze_sha256"]


class TestRejections:
    def _plan(self, tmp_path: Path, capsys, root: Path) -> tuple[object, str, Path]:
        module = _load_script()
        transaction_root = tmp_path / "transactions" / "pdf_identity"
        plan_file = tmp_path / "plan.json"
        code, output = _run(module, "--plan", "--all", "--paper-raw-dir", str(root),
                            "--transaction-root", str(transaction_root),
                            "--plan-file", str(plan_file), capsys=capsys)
        assert code == 0
        return module, plan_file, plan_file.read_text and _plan_hash(output) or _plan_hash(output)

    def test_tampered_plan_rejected(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "paper_raw"
        folder = _workspace(root, PN_A, DOI_A)
        _fitz_pdf(folder / f"{PN_A}.pdf", DOI_A)
        module = _load_script()
        transaction_root = tmp_path / "transactions" / "pdf_identity"
        plan_file = tmp_path / "plan.json"
        code, output = _run(module, "--plan", "--all", "--paper-raw-dir", str(root),
                            "--transaction-root", str(transaction_root),
                            "--plan-file", str(plan_file), capsys=capsys)
        assert code == 0
        plan_hash = _plan_hash(output)
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        plan["papers"][PN_A]["receipt"]["match_status"] = "ambiguous"
        plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        code, output = _run(module, "--receipts-only", "--plan-file", str(plan_file),
                            "--expected-plan-hash", plan_hash,
                            "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
                            capsys=capsys)
        assert code != 0
        assert "tampered" in output or "does not match" in output

    def test_receipt_drift_rejected(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "paper_raw"
        folder = _workspace(root, PN_A, DOI_A)
        _fitz_pdf(folder / f"{PN_A}.pdf", DOI_A)
        module = _load_script()
        transaction_root = tmp_path / "transactions" / "pdf_identity"
        plan_file = tmp_path / "plan.json"
        code, output = _run(module, "--plan", "--all", "--paper-raw-dir", str(root),
                            "--transaction-root", str(transaction_root),
                            "--plan-file", str(plan_file), capsys=capsys)
        assert code == 0
        plan_hash = _plan_hash(output)
        # Workspace changed after the plan: an existing receipt is replaced
        # with different content (existence unchanged, so the inventory
        # guard passes and the per-paper drift guard fires).
        receipt_path = folder / f"{PN_A}.metadata_match.json"
        receipt_path.write_text(
            json.dumps({"match_status": "matched", "changed": True}), encoding="utf-8"
        )
        code, output = _run(module, "--receipts-only", "--plan-file", str(plan_file),
                            "--expected-plan-hash", plan_hash,
                            "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
                            capsys=capsys)
        assert code != 0
        assert "changed since plan" in output or "inventory drifted" in output

    def test_inventory_drift_rejected(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "paper_raw"
        folder = _workspace(root, PN_A, DOI_A)
        _fitz_pdf(folder / f"{PN_A}.pdf", DOI_A)
        module = _load_script()
        transaction_root = tmp_path / "transactions" / "pdf_identity"
        plan_file = tmp_path / "plan.json"
        code, output = _run(module, "--plan", "--all", "--paper-raw-dir", str(root),
                            "--transaction-root", str(transaction_root),
                            "--plan-file", str(plan_file), capsys=capsys)
        assert code == 0
        plan_hash = _plan_hash(output)
        # A new workspace appears after the plan.
        extra = _workspace(root, PN_B, DOI_B)
        _fitz_pdf(extra / f"{PN_B}.pdf", DOI_B)
        code, output = _run(module, "--receipts-only", "--plan-file", str(plan_file),
                            "--expected-plan-hash", plan_hash,
                            "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
                            capsys=capsys)
        assert code != 0
        assert "inventory drifted" in output

    def test_partial_apply_rejected(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "paper_raw"
        folder = _workspace(root, PN_A, DOI_A)
        _fitz_pdf(folder / f"{PN_A}.pdf", DOI_A)
        module = _load_script()
        transaction_root = tmp_path / "transactions" / "pdf_identity"
        plan_file = tmp_path / "plan.json"
        code, output = _run(module, "--plan", "--all", "--paper-raw-dir", str(root),
                            "--transaction-root", str(transaction_root),
                            "--plan-file", str(plan_file), capsys=capsys)
        assert code == 0
        plan_hash = _plan_hash(output)
        code, output = _run(module, "--receipts-only", "--plan-file", str(plan_file),
                            "--expected-plan-hash", plan_hash, "--limit", "1",
                            "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
                            capsys=capsys)
        # Fail closed on partial apply: non-zero exit, nothing written.
        assert code != 0
        assert not (transaction_root / f"pdf_identity_{plan_hash[:16]}" / "journal.json").exists()


class TestAbort:
    def test_abort_restores_assets_and_deletes_new_files(
        self, tmp_path: Path, capsys
    ) -> None:
        root = tmp_path / "paper_raw"
        folder_a = _workspace(root, PN_A, DOI_A)
        _fitz_pdf(folder_a / f"{PN_A}.pdf", DOI_A)
        folder_b = _workspace(root, PN_B, DOI_B)
        # No bibliographic corroboration: different title, different author
        # (and no year on the page) -> the labeled first-page DOI stays
        # medium and the decision is ambiguous.
        _fitz_pdf(folder_b / f"{PN_B}.pdf", DOI_B, title="A Different Migration Paper",
                  author="Zhang Wei")
        # Neither workspace has a receipt or a status file before the
        # migration: abort must delete whatever the migration created.
        assert not (folder_a / f"{PN_A}.metadata_match.json").exists()
        assert not (folder_a / ".import_status.json").exists()
        old_receipt_b = folder_b / f"{PN_B}.metadata_match.json"
        assert not old_receipt_b.exists()

        module = _load_script()
        transaction_root = tmp_path / "transactions" / "pdf_identity"
        plan_file = tmp_path / "plan.json"
        code, output = _run(module, "--plan", "--all", "--paper-raw-dir", str(root),
                            "--transaction-root", str(transaction_root),
                            "--plan-file", str(plan_file), capsys=capsys)
        assert code == 0
        plan_hash = _plan_hash(output)
        code, output = _run(module, "--receipts-only", "--plan-file", str(plan_file),
                            "--expected-plan-hash", plan_hash,
                            "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
                            capsys=capsys)
        assert code == 0
        assert old_receipt_b.exists()

        journal_path = transaction_root / f"pdf_identity_{plan_hash[:16]}" / "journal.json"
        code, output = _run(module, "--abort", str(journal_path),
                            "--paper-raw-dir", str(root), capsys=capsys)
        assert code == 0
        # Restored: files that did not exist before the migration are gone.
        assert not (folder_a / f"{PN_A}.metadata_match.json").exists()
        assert not (folder_a / ".import_status.json").exists()
        assert not old_receipt_b.exists()
        assert not (root / ".pdf_identity_migration.json").exists()
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        assert journal["phase"] == "aborted"


class TestCrashResume:
    def test_resume_from_recorded_substate(self, tmp_path: Path, capsys) -> None:
        root = tmp_path / "paper_raw"
        folder = _workspace(root, PN_A, DOI_A)
        _fitz_pdf(folder / f"{PN_A}.pdf", DOI_A)
        module = _load_script()
        transaction_root = tmp_path / "transactions" / "pdf_identity"
        plan_file = tmp_path / "plan.json"
        code, output = _run(module, "--plan", "--all", "--paper-raw-dir", str(root),
                            "--transaction-root", str(transaction_root),
                            "--plan-file", str(plan_file), capsys=capsys)
        assert code == 0
        plan_hash = _plan_hash(output)
        journal_path = transaction_root / f"pdf_identity_{plan_hash[:16]}" / "journal.json"

        # Simulate a crash: the journal exists with a paper stuck at
        # "receipt_written" (receipt written, status not yet updated).
        journal = {
            "schema_version": "1.0",
            "run_id": journal_path.parent.name,
            "phase": "receipts_applying",
            "plan_path": str(plan_file),
            "plan_content_hash": plan_hash,
            "plan_file_sha256": "x",
            "extractor_version": "2.0",
            "decision_policy_version": "2.0",
            "papers": {
                PN_A: {
                    "substate": "receipt_written",
                    "status": "pending",
                    "old_receipt_sha256": None,
                    "new_receipt_sha256": "x",
                    "old_freeze_sha256": None,
                    "old_freeze_existed": False,
                    "freeze_eligible": True,
                    "freeze_block_reason": "",
                    "target_revision": 1,
                    "target_frozen_at": "2026-01-01T00:00:00+00:00",
                    "target_freeze_sha256": None,
                    "failure_reason": "",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            },
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "last_error": None,
        }
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        # The receipt was already written by the "crashed" run.
        receipt = json.loads(plan_file.read_text(encoding="utf-8"))["papers"][PN_A]["receipt"]
        (folder / f"{PN_A}.metadata_match.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        code, output = _run(module, "--receipts-only", "--plan-file", str(plan_file),
                            "--expected-plan-hash", plan_hash,
                            "--paper-raw-dir", str(root), "--transaction-root", str(transaction_root),
                            capsys=capsys)
        assert code == 0, output
        status = json.loads((folder / ".import_status.json").read_text(encoding="utf-8"))
        assert status["metadata"]["state"] == "matched"
        stored = json.loads(journal_path.read_text(encoding="utf-8"))
        assert stored["phase"] == "receipts_applied"
        assert stored["papers"][PN_A]["status"] == "status_written"
