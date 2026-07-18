"""Reconcile durable commit journals against disk state."""
from __future__ import annotations
from pathlib import Path
from src.ingest.commit import resume_commit, validate_committed_state
from src.ingest.transactions import (
    ACTIVE_PHASES,
    CommitJournalStore,
    find_active_transaction_for_paper,
    ordered_transaction_locks,
)
from src.ingest.workspace import PaperRawWorkspace

def reconcile_commits(*,transactions_dir: Path,paper_raw_root: Path,papers_dir: Path,ledger_path: Path,catalog_root: Path,paper_number: str|None=None,apply: bool=True)->list[dict]:
    store=CommitJournalStore(transactions_dir,paper_raw_root=paper_raw_root,papers_root=papers_dir); results=[]
    journals = store.load_all()
    active_numbers = sorted({
        str(journal["paper_number"])
        for _, journal in journals
        if journal.get("phase") in ACTIVE_PHASES
    }, key=int)
    for number in active_numbers:
        active = find_active_transaction_for_paper(
            transactions_dir,
            number,
            paper_raw_root=paper_raw_root,
            papers_root=papers_dir,
        )
        if active is None or active[0] != "commit":
            raise RuntimeError(
                f"transaction_repair_required: active commit preflight failed for {number}"
            )
    for path,journal in journals:
        if paper_number and journal.get("paper_number")!=paper_number: continue
        phase = journal.get("phase")
        if phase in {"category_reconcile_requested", "source_deleted", "complete"}:
            from src.library.paper_number_ledger import PaperNumberLedger
            validate_committed_state(
                journal,
                papers_dir=papers_dir,
                ledger=PaperNumberLedger(ledger_path),
            )
            source_exists = Path(journal["source_workspace"]).exists()
            if phase in {"source_deleted", "complete"} and source_exists:
                raise RuntimeError(f"{phase} phase contradicts existing source workspace")
        if not apply:
            status = "already_complete" if phase == "complete" else "recoverable"
            results.append({**journal,"status":status}); continue
        if phase=="complete":
            results.append({**journal,"status":"already_complete"}); continue
        source=Path(journal["source_workspace"])
        workspace=None
        if source.exists() and journal.get("phase") in {"prepared","staging_complete"}:
            workspace=PaperRawWorkspace.from_path(source)
        with ordered_transaction_locks(store,[journal["paper_number"]]):
            results.append(resume_commit(journal,store=store,workspace=workspace,papers_dir=papers_dir,ledger_path=ledger_path,catalog_root=catalog_root,paper_raw_root=paper_raw_root))
    return results
