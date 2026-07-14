"""Job-level BibTeX export and citation validation.

Each BibTeX entry is generated from the **job-local** copied article metadata
(``write/jobs/<job_id>/article/<paper_number>/*.metadata.json``) and written into
the job-local ``tex/references.bib`` file. The job-level export/validate paths
never touch the global ``Catalog()`` / ``PaperLibrary()``; only
``validate_catalog_citations`` audits the live formal library.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src import bib as bibmod
from src.catalog_folders.reader import create_safe_catalog_reader
from src.services.paper_library import PaperLibrary
from src.writer.catalog_matcher import load_selected
from src.writer.job_manager import JobManager
from src.metadata.citation_readiness import citation_key_from_metadata, validate_citation_ready


def resolve_work_dir(jdir: Path, work_dir: str) -> Path:
    """Resolve a manifest ``work_dir`` value to an absolute article path.

    New manifests store job-relative paths (e.g. ``article/0000000000000001``);
    older manifests stored absolute paths. Both are accepted: an absolute path
    is used as-is, a relative one is joined to the job root. This keeps the job
    portable (the manifest has no machine-specific absolute path).
    """
    p = Path(work_dir)
    return p if p.is_absolute() else (jdir / p)


def load_workset_manifest(job_id: str, jm: JobManager | None = None) -> dict[str, Path]:
    """Read ``planning/workset_manifest.json`` and return ``{paper_name: work_dir}``.

    Raises ``RuntimeError`` if the manifest is missing — i.e. ``prepare-workset
    --apply`` has not been run. Job-level BibTeX/cite-key generation is strictly
    job-local, so the manifest is the entry point to the copied article metadata.
    ``work_dir`` values are resolved against the job root (see ``resolve_work_dir``).
    """
    jm = jm or JobManager()
    jdir = jm.job_dir(job_id)
    manifest_path = jdir / "planning" / "workset_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(
            "workset_manifest.json not found. "
            "Run `scripts/create_write_job.py --paper-nums ... --apply` first.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mapping: dict[str, Path] = {}
    for entry in manifest.get("copied", []):
        pid = entry.get("paper_name", "")
        wd = entry.get("work_dir", "")
        if pid and wd:
            mapping[pid] = resolve_work_dir(jdir, wd)
    return mapping


def _read_article_metadata(work_dir: Path) -> dict | None:
    """Read the single ``*.metadata.json`` from a copied article work_dir.

    Returns ``None`` when the copied article has no metadata file.
    """
    metas = sorted(Path(work_dir).glob("*.metadata.json"))
    if not metas:
        return None
    return json.loads(metas[0].read_text(encoding="utf-8"))


def job_local_bib_keys(pid_to_work_dir: dict[str, Path]) -> dict[str, str]:
    """Map ``paper_name -> bib_key`` derived from job-local article metadata.

    No global ``Catalog`` / ``PaperLibrary`` access. Falls back to
    ``sanitize_paper_name(pid)`` when the copied article has no metadata, matching
    the metadata-empty branch of ``bib_key_for_entry``. This is the single source
    of truth shared by ``export_job_bib`` / ``validate_job_bib`` /
    ``deep_read`` / ``copy_figures`` so every cite key agrees.
    """
    keys: dict[str, str] = {}
    for pid, wd in pid_to_work_dir.items():
        meta = _read_article_metadata(Path(wd))
        if not meta:
            raise RuntimeError(f"job-local article metadata missing for paper_name={pid}")
        keys[pid] = bibmod.sanitize_paper_name(citation_key_from_metadata(meta))
    return keys


def _metadata_for_entry(entry: dict, library: PaperLibrary) -> dict:
    number = str(entry.get("paper_number") or "")
    if not number:
        raise RuntimeError("catalog entry lacks paper_number; citation lookup cannot fall back to paper_name")
    metadata = library.load_metadata(number)
    if not metadata:
        raise RuntimeError(f"metadata missing for paper_number={number}; Catalog cannot substitute for citation data")
    return metadata


def validate_catalog_citations(catalog_data: dict | None = None) -> list[str]:
    """Validate that catalog entries can produce complete BibTeX records."""
    cat = catalog_data or {"papers": create_safe_catalog_reader().list_papers(["all"])}
    library = PaperLibrary()
    errors: list[str] = []
    seen: set[str] = set()

    for entry in cat.get("papers", []):
        ctx = f"paper_name={entry.get('paper_name', '?')}"
        try:
            metadata = _metadata_for_entry(entry, library)
        except RuntimeError as exc:
            errors.append(f"{ctx} {exc}")
            continue
        try:
            bib_key = bibmod.sanitize_paper_name(citation_key_from_metadata(metadata))
        except ValueError as exc:
            errors.append(f"{ctx} {exc}")
            continue
        if not bib_key:
            errors.append(f"{ctx} missing bib_key")
        elif bib_key in seen:
            errors.append(f"{ctx} duplicate bib_key: {bib_key}")
        else:
            seen.add(bib_key)

        bibtex = bibmod.bibtex_from_metadata(metadata,key=bib_key).strip()
        if not bibtex or not bibtex.startswith("@"):
            errors.append(f"{ctx} failed to generate BibTeX entry")
            continue

        match = re.match(r"@\w+\s*\{\s*([^,\s]+)", bibtex)
        if match and match.group(1) != bib_key:
            errors.append(f"{ctx} bibtex entry key({match.group(1)}) != bib_key({bib_key})")

        for field in ("title", "author", "year"):
            if not re.search(rf"\b{field}\s*=", bibtex, re.IGNORECASE):
                errors.append(f"{ctx} bibtex missing {field} field")

        doi = str(((metadata.get("identifiers") or {}).get("doi") or "")).strip()
        if doi and "doi" not in bibtex.lower():
            errors.append(f"{ctx} metadata has DOI but BibTeX does not include doi")

    return errors


def export_job_bib(
    job_id: str,
    bib_keys: list[str] | None = None,
    jm: JobManager | None = None,
) -> dict:
    """Export selected papers into the job-local ``tex/references.bib`` file.

    BibTeX entries are generated **only** from the copied article metadata in
    ``write/jobs/<job_id>/article/<paper_number>/*.metadata.json`` — never from
    the global ``Catalog`` / ``PaperLibrary``. Requires ``prepare-workset --apply``
    (so ``workset_manifest.json`` exists). Missing article metadata or DOI raises
    rather than silently emitting an empty ``references.bib``.
    """
    jm = jm or JobManager()
    job_dir = jm.job_dir(job_id)
    pid_to_work_dir = load_workset_manifest(job_id, jm)
    key_to_pid = {v: k for k, v in job_local_bib_keys(pid_to_work_dir).items()}

    if bib_keys is None:
        selected = load_selected(job_id, jm)
        if selected.get("selection_status") != "confirmed":
            raise RuntimeError("selected_papers.json is not confirmed; refusing to export references.bib")
        pids = [item.get("paper_name", "") for item in selected.get("selected_papers", [])]
    else:
        pids = [key_to_pid[k] for k in bib_keys if k in key_to_pid]

    blocks: list[str] = []
    for pid in pids:
        if pid not in pid_to_work_dir:
            raise RuntimeError(
                f"paper_name={pid} not in workset_manifest; run prepare-workset --apply.")
        meta = _read_article_metadata(pid_to_work_dir[pid])
        if not meta:
            raise RuntimeError(
                f"job-local article metadata missing for paper_name={pid}; "
                "run prepare-workset --apply to copy it.")
        readiness = validate_citation_ready(meta)
        if not readiness.ready:
            raise RuntimeError(f"paper_name={pid} metadata is not citation-ready: {'; '.join(readiness.errors)}")
        key = job_local_bib_keys({pid: pid_to_work_dir[pid]})[pid]
        blocks.append(bibmod.bibtex_from_metadata(meta, key=key))

    out = job_dir / "tex" / "references.bib"
    out.parent.mkdir(parents=True, exist_ok=True)
    header = f"% Generated for write/jobs/{job_id} from job-local article metadata; {len(blocks)} BibTeX entries.\n\n"
    out.write_text(header + "\n\n".join(blocks) + "\n", encoding="utf-8")
    return {"references_bib": str(out), "count": len(blocks)}


def validate_job_bib(job_id: str, jm: JobManager | None = None) -> list[str]:
    """Validate job-local ``references.bib`` against confirmed selected papers."""
    jm = jm or JobManager()
    job_dir = jm.job_dir(job_id)
    bib_path = job_dir / "tex" / "references.bib"
    errors: list[str] = []
    if not bib_path.exists():
        return ["missing tex/references.bib"]

    raw = bib_path.read_text(encoding="utf-8")
    blocks = bibmod.parse_blocks(raw)
    keys = re.findall(r"@\w+\s*\{\s*([^,\s]+)", raw)
    if len(keys) != len(set(keys)):
        errors.append("references.bib contains duplicate entry keys")

    selected = load_selected(job_id, jm)
    if selected.get("selection_status") == "confirmed":
        try:
            pid_to_work_dir = load_workset_manifest(job_id, jm)
        except RuntimeError as exc:
            errors.append(str(exc))
            return errors
        bib_map = job_local_bib_keys(pid_to_work_dir)
        selected_keys: set[str] = set()
        for item in selected.get("selected_papers", []):
            pid = item.get("paper_name", "")
            if pid in bib_map:
                selected_keys.add(bib_map[pid])
        extra = set(blocks.keys()) - selected_keys
        if extra:
            errors.append(f"references.bib contains entries outside selected_papers: {sorted(extra)}")
    return errors


def _extract_cite_keys(tex_text: str) -> set[str]:
    """Extract keys from uncommented LaTeX cite commands."""
    keys: set[str] = set()
    for line in tex_text.splitlines():
        if line.lstrip().startswith("%"):
            continue
        code = line.split("%", 1)[0]
        for match in re.finditer(r"\\cite[a-zA-Z]*\s*\{([^}]*)\}", code):
            for key in match.group(1).split(","):
                key = key.strip()
                if key:
                    keys.add(key)
    return keys


def validate_job_citations(job_id: str, jm: JobManager | None = None) -> dict:
    """Validate that TeX citations and job-local BibTeX keys agree."""
    jm = jm or JobManager()
    job_dir = jm.job_dir(job_id)
    bib_path = job_dir / "tex" / "references.bib"
    bib_keys = set(bibmod.parse_blocks(bib_path.read_text(encoding="utf-8")).keys()) if bib_path.exists() else set()

    cited: set[str] = set()
    tex_dir = job_dir / "tex"
    if tex_dir.exists():
        for tex in tex_dir.rglob("*.tex"):
            cited |= _extract_cite_keys(tex.read_text(encoding="utf-8"))

    missing = cited - bib_keys
    unused = bib_keys - cited
    return {
        "cited_keys": sorted(cited),
        "bib_keys": sorted(bib_keys),
        "missing_in_bib": sorted(missing),
        "unused_in_bib": sorted(unused),
        "valid": len(missing) == 0,
    }


def portability_check(job_id: str, jm: JobManager | None = None) -> dict:
    """Check whether the job-local TeX project is self-contained."""
    jm = jm or JobManager()
    job_dir = jm.job_dir(job_id)
    tex_dir = job_dir / "tex"
    errors: list[str] = []

    def _resolve(base: Path, ref: str) -> Path:
        return (base / ref).resolve()

    if tex_dir.exists():
        for tex in tex_dir.rglob("*.tex"):
            text = tex.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.lstrip().startswith("%"):
                    continue
                for match in re.finditer(r"\\(?:bibliography|addbibresource)\s*\{([^}]*)\}", line):
                    ref = match.group(1).strip()
                    candidate = _resolve(tex.parent, ref)
                    if not candidate.exists() and not candidate.with_suffix(".bib").exists():
                        errors.append(f"{tex.name}: bibliography reference not found in tex project: {ref}")
                for match in re.finditer(r"\\(?:input|include)\s*\{([^}]*)\}", line):
                    ref = match.group(1).strip()
                    candidate = _resolve(tex.parent, ref)
                    if not (candidate.exists() or candidate.with_suffix(".tex").exists()):
                        errors.append(f"{tex.name}: input/include reference not found: {ref}")
                for match in re.finditer(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", line):
                    ref = match.group(1).strip()
                    resolved = _resolve(tex.parent, ref)
                    try:
                        resolved.relative_to(job_dir.resolve())
                    except ValueError:
                        errors.append(f"{tex.name}: graphics reference points outside job directory: {ref}")
                        continue
                    if not resolved.exists():
                        errors.append(f"{tex.name}: graphics file does not exist inside job directory: {ref}")

    return {
        "portable": len(errors) == 0,
        "errors": errors,
        "note": "job directory is self-contained" if not errors else "external or missing references found",
    }
