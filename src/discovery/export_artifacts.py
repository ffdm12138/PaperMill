"""Candidate export artifacts: idempotent JSONL + manifest emit/validate.

Extracted from ``pending_queue`` (which keeps only candidate-journal
orchestration).  Export idempotency requires matching manifest and JSONL
identity, hash, size, and record count; validation caching is bound to both
artifact stat fingerprints, never TTL.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.discovery.contracts.page_journal import stable_hash
from src.discovery.stores.journal_drain_index import JournalDrainIndex
from src.utils.atomic_io import atomic_write_json_unlocked, atomic_write_text
from src.utils.identifiers import normalize_doi
from src.utils.timestamps import utc_now_iso as _now_iso


def export_id_for(candidate_id: str) -> str:
    return stable_hash("export", candidate_id, length=40)


def export_paths(exports_dir: Path, export_id: str) -> tuple[Path, Path]:
    return Path(exports_dir) / f"{export_id}.jsonl", Path(exports_dir) / f"{export_id}.manifest.json"


@dataclass(frozen=True)
class ExportValidationResult:
    valid: bool
    reason: str = ""


def validate_export_artifacts(
    *,
    manifest_path: Path,
    jsonl_path: Path,
    expected_candidate_id: str,
    expected_export_id: str,
    expected_doi: str | None,
    export_root: Path,
) -> ExportValidationResult:
    """Validate identity, containment, bytes, and record count for an export."""
    root = Path(export_root).absolute()
    manifest_path = Path(manifest_path).absolute()
    jsonl_path = Path(jsonl_path).absolute()
    for label, path in (("manifest", manifest_path), ("JSONL", jsonl_path)):
        try:
            path.relative_to(root)
        except ValueError:
            return ExportValidationResult(False, f"{label} path escapes export root")
        current = root
        for part in path.relative_to(root).parts:
            current /= part
            if current.is_symlink():
                return ExportValidationResult(False, f"{label} path is a symlink")
        if not path.is_file():
            return ExportValidationResult(False, f"{label} missing")
        try:
            path.resolve(strict=True).relative_to(root.resolve(strict=True))
        except (OSError, ValueError):
            return ExportValidationResult(False, f"{label} resolved path escapes export root")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ExportValidationResult(False, f"manifest unreadable: {type(exc).__name__}")
    if not isinstance(manifest, dict):
        return ExportValidationResult(False, "manifest invalid")
    if str(manifest.get("export_id") or "") != expected_export_id:
        return ExportValidationResult(False, "export_id mismatch")
    if str(manifest.get("candidate_id") or "") != expected_candidate_id:
        return ExportValidationResult(False, "candidate_id mismatch")
    recorded_path = Path(str(manifest.get("jsonl_path") or manifest.get("export_path") or "")).absolute()
    if recorded_path != jsonl_path:
        return ExportValidationResult(False, "artifact path mismatch")
    try:
        raw = jsonl_path.read_bytes()
        lines = [line for line in raw.splitlines() if line.strip()]
        records = [json.loads(line) for line in lines]
    except Exception as exc:
        return ExportValidationResult(False, f"JSONL unreadable: {type(exc).__name__}")
    if len(records) != 1 or not isinstance(records[0], dict):
        return ExportValidationResult(False, "JSONL record_count mismatch")
    expected = normalize_doi(expected_doi or "")
    manifest_doi = normalize_doi(manifest.get("normalized_doi") or manifest.get("doi") or "")
    payload_doi = normalize_doi(records[0].get("doi") or "")
    if expected and (manifest_doi != expected or payload_doi != expected):
        return ExportValidationResult(False, "DOI mismatch")
    artifact = manifest.get("artifact") or {}
    if not isinstance(artifact, dict):
        return ExportValidationResult(False, "artifact metadata missing")
    if int(artifact.get("size_bytes", -1)) != len(raw):
        return ExportValidationResult(False, "artifact size mismatch")
    if str(artifact.get("sha256") or "") != hashlib.sha256(raw).hexdigest():
        return ExportValidationResult(False, "artifact hash mismatch")
    if int(artifact.get("record_count", -1)) != 1:
        return ExportValidationResult(False, "artifact record_count mismatch")
    return ExportValidationResult(True)


def export_candidate_once(exports_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    export_id = export_id_for(record["candidate_id"])
    jsonl_path, manifest_path = export_paths(exports_dir, export_id)
    if manifest_path.exists() != jsonl_path.exists():
        raise RuntimeError("export_artifact_corrupt: manifest/JSONL pair incomplete")
    if manifest_path.exists() and jsonl_path.exists():
        validation = validate_export_artifacts(
            manifest_path=manifest_path,
            jsonl_path=jsonl_path,
            expected_candidate_id=str(record["candidate_id"]),
            expected_export_id=export_id,
            expected_doi=normalize_doi((record.get("candidate") or {}).get("doi") or ""),
            export_root=exports_dir,
        )
        if not validation.valid:
            raise RuntimeError(f"export_artifact_corrupt: {validation.reason}")
        return {
            "export_id": export_id,
            "export_path": jsonl_path.as_posix(),
            "manifest_path": manifest_path.as_posix(),
            "reconciled": True,
        }
    payload = record.get("candidate") if isinstance(record.get("candidate"), dict) else {}
    jsonl_text = json.dumps(payload, ensure_ascii=False) + "\n"
    atomic_write_text(jsonl_path, jsonl_text)
    jsonl_bytes = jsonl_path.read_bytes()
    manifest = {
        "schema_version": "1.0",
        "export_id": export_id,
        "candidate_id": record["candidate_id"],
        "page_id": record.get("page_id"),
        "keyword_id": record.get("keyword_id"),
        "provider": record.get("provider"),
        "normalized_doi": normalize_doi(payload.get("doi") or ""),
        "jsonl_path": jsonl_path.as_posix(),
        "provider_identity": {"provider": record.get("provider")},
        "artifact": {
            "path": jsonl_path.as_posix(),
            "sha256": hashlib.sha256(jsonl_bytes).hexdigest(),
            "size_bytes": len(jsonl_bytes),
            "record_count": 1,
        },
        "exported_at": _now_iso(),
    }
    atomic_write_json_unlocked(manifest_path, manifest, indent=2)
    return {
        "export_id": export_id,
        "export_path": jsonl_path.as_posix(),
        "manifest_path": manifest_path.as_posix(),
        "reconciled": False,
    }


def inspect_emitted_primary_export(
    item: dict[str, Any],
    doi: str,
    *,
    exports_dir: Path,
) -> tuple[bool, str]:
    manifest_path = str(item.get("manifest_path") or item.get("export_manifest_path") or "").strip()
    export_path = str(item.get("export_path") or "").strip()
    export_id = str(item.get("export_id") or "").strip()
    candidate_id = str(item.get("candidate_id") or "").strip()
    if not export_id:
        return False, "emitted export_id missing"
    expected_jsonl, expected_manifest = export_paths(exports_dir, export_id)
    if Path(manifest_path).absolute() != expected_manifest.absolute():
        return False, "manifest path is not canonical for trusted export root"
    if export_path and Path(export_path).absolute() != expected_jsonl.absolute():
        return False, "JSONL path is not canonical for trusted export root"
    if not manifest_path or not expected_manifest.is_file():
        return False, "emitted export manifest missing"
    if export_path:
        jsonl_path = Path(export_path)
    else:
        try:
            manifest_data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            jsonl_path = Path(str(manifest_data.get("jsonl_path") or manifest_data.get("export_path") or ""))
        except Exception:
            jsonl_path = Path(manifest_path).with_name(f"{export_id}.jsonl")
    validation = validate_export_artifacts(
        manifest_path=Path(manifest_path),
        jsonl_path=jsonl_path,
        expected_candidate_id=candidate_id,
        expected_export_id=export_id,
        expected_doi=doi,
        export_root=exports_dir,
    )
    return validation.valid, validation.reason


def inspect_emitted_primary_export_cached(
    journal_index: JournalDrainIndex,
    item: dict[str, Any],
    doi: str,
    *,
    exports_dir: Path,
) -> tuple[bool, str]:
    """Cache validation only while both artifact fingerprints are unchanged."""
    export_id = str(item.get("export_id") or "").strip()
    jsonl_path, manifest_path = export_paths(exports_dir, export_id)

    def stat_fingerprint(path: Path) -> tuple[int, int]:
        try:
            stat = path.stat()
        except OSError:
            return -1, -1
        return stat.st_size, stat.st_mtime_ns

    manifest_size, manifest_mtime = stat_fingerprint(manifest_path)
    jsonl_size, jsonl_mtime = stat_fingerprint(jsonl_path)
    key = (
        str(manifest_path.absolute()), manifest_size, manifest_mtime,
        str(jsonl_path.absolute()), jsonl_size, jsonl_mtime,
    )
    cached = journal_index.get_cached_emitted_validation(key)
    if cached is not None:
        return cached
    result = inspect_emitted_primary_export(item, doi, exports_dir=exports_dir)
    manifest_identity, jsonl_identity = key[0], key[3]
    journal_index.set_cached_emitted_validation(
        key, result, manifest_identity=manifest_identity,
        jsonl_identity=jsonl_identity,
    )
    return result


