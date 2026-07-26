"""MigrationReceiptStoreV4 — idempotent seed import receipts.

Two receipt families live here, each with exactly one writer:

* Drain receipts (``write_receipt``/``has_receipt``/``get_status``) —
  written by the normal-discovery legacy-seed drain
  (``pending_queue._drain_pending_store_candidates``) to durably record
  that a migrated ``legacy_candidate_seed`` candidate was consumed.
  Idempotent overwrite by design: re-draining the same seed refreshes
  its receipt.
* Strict post-cutover seed receipts (``write_seed_receipt``) — written
  only by ``post_cutover_reconciliation`` and carrying the full evidence
  closure for one migrated legacy candidate seed.  Seed receipts are
  create-if-absent: an identical rewrite is an idempotent success, a
  conflicting rewrite raises :class:`MigrationReceiptConflictError`, and
  a corrupt existing receipt raises
  :class:`MigrationReceiptCorruptError`` — never a silent overwrite.

Receipt files are named ``<seed_id>.json`` under the receipts directory.
The default directory is ``<workspace.root>/migration_receipts``; callers
that must keep receipts outside the manifest-hashed generation tree pass an
explicit ``receipts_dir`` (e.g. ``data/discovery/migrations/<mid>.receipts``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping

from src.discovery.workspace import DiscoveryWorkspace


ReceiptStatus = Literal[
    "imported", "already_existing", "duplicate_seed",
    "invalid_doi", "unresolved", "failed",
]

SEED_RECEIPT_SCHEMA_VERSION = "1.0"

SEED_RECEIPT_OUTCOMES = frozenset({
    "staged_new",
    "reused_existing",
    "duplicate_existing",
    "terminal_with_evidence",
})

SEED_RECEIPT_REQUIRED_FIELDS = frozenset({
    "schema_version",
    "migration_id",
    "seed_id",
    "candidate_id",
    "normalized_doi",
    "outcome",
    "paper_number",
    "metadata_sha256",
    "match_receipt_sha256",
    "ledger_entry_sha256",
    "verified_at",
})


class MigrationReceiptConflictError(RuntimeError):
    """A different receipt already exists for the same seed_id."""


class MigrationReceiptCorruptError(RuntimeError):
    """An existing receipt file is unreadable or schema-violating."""


def validate_seed_receipt(record: Mapping[str, Any]) -> None:
    """Fail closed unless ``record`` is a strict seed receipt."""
    missing = SEED_RECEIPT_REQUIRED_FIELDS - set(record)
    if missing:
        raise ValueError(f"seed receipt missing fields: {sorted(missing)}")
    if record["schema_version"] != SEED_RECEIPT_SCHEMA_VERSION:
        raise ValueError(
            f"seed receipt schema_version must be {SEED_RECEIPT_SCHEMA_VERSION!r}, "
            f"got {record['schema_version']!r}"
        )
    if record["outcome"] not in SEED_RECEIPT_OUTCOMES:
        raise ValueError(
            f"seed receipt outcome must be one of {sorted(SEED_RECEIPT_OUTCOMES)}, "
            f"got {record['outcome']!r}"
        )
    for field in SEED_RECEIPT_REQUIRED_FIELDS - {"schema_version", "outcome"}:
        if not isinstance(record[field], str):
            raise ValueError(f"seed receipt field {field!r} must be a string")
    for field in ("migration_id", "seed_id", "candidate_id", "normalized_doi",
                  "paper_number", "metadata_sha256", "ledger_entry_sha256",
                  "verified_at"):
        if not record[field]:
            raise ValueError(f"seed receipt field {field!r} must be non-empty")


class MigrationReceiptStoreV4:
    """Tracks which legacy candidate seeds have been imported.

    Stores one receipt per seed_id under the receipts directory to ensure
    idempotent re-import and post-cutover reconciliation.
    """

    def __init__(
        self,
        workspace: DiscoveryWorkspace | None = None,
        *,
        receipts_dir: Path | None = None,
    ) -> None:
        if receipts_dir is None:
            if workspace is None:
                raise ValueError(
                    "MigrationReceiptStoreV4 requires a workspace or an "
                    "explicit receipts_dir"
                )
            receipts_dir = workspace.root / "migration_receipts"
        self._workspace = workspace
        self._dir = Path(receipts_dir)

    @property
    def workspace(self) -> DiscoveryWorkspace | None:
        return self._workspace

    @property
    def receipts_dir(self) -> Path:
        return self._dir

    # ── drain receipts (legacy-seed drain channel) ────────────────────

    def write_receipt(
        self, seed_id: str, status: ReceiptStatus, metadata: dict[str, Any] | None = None
    ) -> Path:
        """Record a seed import receipt. Idempotent — overwrites same seed_id."""
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{seed_id}.json"
        payload_data: dict[str, Any] = {
            "seed_id": seed_id,
            "status": status,
            "metadata": metadata or {},
        }
        payload = json.dumps(payload_data, ensure_ascii=False, indent=2)
        raw = payload.encode("utf-8")
        tmp = path.with_suffix(path.suffix + ".tmp")

        try:
            with tmp.open("wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(str(tmp), str(path))
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

        return path

    def has_receipt(self, seed_id: str) -> bool:
        """Check if a seed has already been processed."""
        return (self._dir / f"{seed_id}.json").is_file()

    def get_status(self, seed_id: str) -> ReceiptStatus | None:
        """Get the status of a seed import, or None if not processed."""
        path = self._dir / f"{seed_id}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        return data.get("status")

    def count_by_status(self) -> dict[str, int]:
        """Count receipts by status."""
        counts: dict[str, int] = {}
        if not self._dir.is_dir():
            return counts
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                status = data.get("status", "unknown")
                counts[status] = counts.get(status, 0) + 1
            except (json.JSONDecodeError, OSError):
                pass
        return counts

    # ── strict post-cutover seed receipts ─────────────────────────────

    def write_seed_receipt(self, record: Mapping[str, Any]) -> Path:
        """Create a strict seed receipt; never silently overwrite.

        * absent  → create atomically;
        * present with identical evidence → idempotent success (the
          volatile ``verified_at`` timestamp is excluded from the
          comparison; the original receipt is kept);
        * present with different evidence →
          :class:`MigrationReceiptConflictError`.
        """
        validate_seed_receipt(record)
        seed_id = str(record["seed_id"])
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{seed_id}.json"
        raw = (
            json.dumps(dict(record), ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")

        def _existing_matches() -> bool:
            existing_raw = path.read_bytes()
            if existing_raw == raw:
                return True
            try:
                existing = json.loads(existing_raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return False
            if not isinstance(existing, dict):
                return False
            strip = ("verified_at",)
            return (
                {k: v for k, v in existing.items() if k not in strip}
                == {k: v for k, v in dict(record).items() if k not in strip}
            )

        if path.exists():
            if _existing_matches():
                return path
            raise MigrationReceiptConflictError(
                f"conflicting seed receipt already exists: {path}"
            )

        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("xb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(str(tmp), str(path))
        except FileExistsError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if _existing_matches():
                return path
            raise MigrationReceiptConflictError(
                f"conflicting seed receipt already exists: {path}"
            ) from None
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return path

    def read_seed_receipt(self, seed_id: str) -> dict[str, Any] | None:
        """Read a strict seed receipt.

        Returns ``None`` when absent; raises
        :class:`MigrationReceiptCorruptError` when the file exists but is
        unreadable or schema-violating.
        """
        path = self._dir / f"{seed_id}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise MigrationReceiptCorruptError(
                f"corrupt seed receipt {path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise MigrationReceiptCorruptError(
                f"corrupt seed receipt {path}: payload is not an object"
            )
        try:
            validate_seed_receipt(data)
        except ValueError as exc:
            raise MigrationReceiptCorruptError(
                f"corrupt seed receipt {path}: {exc}"
            ) from exc
        return data

    def iter_seed_receipts(self) -> Iterator[dict[str, Any]]:
        """Yield every strict seed receipt, sorted by seed_id (fail closed)."""
        if not self._dir.is_dir():
            return
        for path in sorted(self._dir.glob("*.json")):
            yield self.read_seed_receipt(path.stem)  # type: ignore[misc]

    def count_seed_receipts(self) -> int:
        """Number of seed receipt files currently on disk."""
        if not self._dir.is_dir():
            return 0
        return sum(1 for _ in self._dir.glob("*.json"))
