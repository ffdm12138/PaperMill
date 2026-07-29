"""FastAPI service for the pure v2 paper_raw library.

Service layering: the security middleware handles ONLY API-key auth and
security headers — it never initializes business services.  Each domain
initializes lazily on first use through its own getter:

- BaseServices    : ``_get_library()``, ``_get_prompt_builder()``,
  ``_get_job_manager()`` (papers, prompts, write jobs)
- CatalogServices : ``_get_catalog()`` (catalog folder browsing/validation)
- DiscoveryServices: resolved per-request by ``_discovery_state()`` and
  the ``/status/discovery`` endpoint; a fresh install reports typed
  ``uninitialized`` while damaged state reports typed ``corrupt`` /
  ``incomplete`` — never conflated.
"""
from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import API_CORS_ORIGINS, API_HOST, API_PORT, CATALOG_FOLDER_ROOT, MINERU_API_KEY, PAPERS_DIR
from src.catalog_folders.reader import CatalogFolderReader, create_safe_catalog_reader
from src.catalog_folders.paper_library import PaperLibrary
import src.discovery.runtime_context as runtime_context
from src.discovery.maintenance_gate import discovery_maintenance_block_reason
from src.discovery.runtime_context import (
    DiscoveryRuntimeCorrupt,
    DiscoveryRuntimeIncomplete,
    DiscoveryRuntimeMaintenance,
    DiscoveryRuntimeNotInitialized,
    DiscoveryRuntimeUnavailableError,
)
from src.utils.naming import validate_job_id, validate_paper_name
from src.prompt_builder import PromptBuilder
from src.metadata.citation import bibtex_from_metadata
from src.writer.bib_manager import portability_check, validate_catalog_citations, validate_job_citations
from src.writer.catalog_matcher import confirm_selected_papers, match_catalog
from src.writer.deep_reader import deep_read, mark_deep_reading_filled, prepare_workset
from src.writer.figure_manager import copy_figures
from src.writer.job_manager import JobManager
from src.writer.job_validator import validate_job
from src.writer.story_builder import build_story, mark_story_filled
from src.writer.tex_project import build_tex, mark_tex_content_filled
from src.writer.topic_parser import normalize_task


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle hook.

    Business services are deliberately NOT initialized here or in any
    middleware; each domain lazily initializes on first request that
    needs it.  ``app.state`` carries only lifecycle metadata.
    """
    app.state.services_initialized_on_demand = True
    yield


app = FastAPI(
    title="MinerU v2 文献资产库",
    description="paper_raw 入库 + 分类目录浏览 + 按需全文复制",
    version="4.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=API_CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)

# Lazy per-domain service singletons — None at import so tests can
# monkeypatch and ``import src.server`` never touches the real library.
catalog = None
library = None
prompt_builder = None
job_manager = None


def _get_catalog() -> CatalogFolderReader:
    """CatalogServices: the catalog folder reader, created on first use."""
    global catalog
    if catalog is None:
        catalog = create_safe_catalog_reader()
    return catalog


def _get_library() -> PaperLibrary:
    """BaseServices: the formal paper library."""
    global library
    if library is None:
        library = PaperLibrary(papers_dir=PAPERS_DIR)
    return library


def _get_prompt_builder() -> PromptBuilder:
    """BaseServices: prompt builder over catalog + library."""
    global prompt_builder
    if prompt_builder is None:
        prompt_builder = PromptBuilder(catalog=_get_catalog(), library=_get_library())
    return prompt_builder


def _get_job_manager() -> JobManager:
    """BaseServices: write-job manager."""
    global job_manager
    if job_manager is None:
        job_manager = JobManager()
    return job_manager


def _discovery_state() -> dict:
    """Probe the discovery runtime and classify its state.

    Returns ``{"state": "ready"|"uninitialized"|"corrupt"|"incomplete"|
    "maintenance", "reason": str|None}``.  Never raises.
    """
    reason = discovery_maintenance_block_reason()
    if reason is not None:
        return {"state": "maintenance", "reason": reason}
    try:
        runtime_context.resolve_active_runtime()
    except DiscoveryRuntimeNotInitialized as exc:
        return {"state": "uninitialized", "reason": str(exc)}
    except DiscoveryRuntimeIncomplete as exc:
        return {"state": "incomplete", "reason": str(exc)}
    except DiscoveryRuntimeCorrupt as exc:
        return {"state": "corrupt", "reason": str(exc)}
    except DiscoveryRuntimeMaintenance as exc:
        return {"state": "maintenance", "reason": str(exc)}
    except DiscoveryRuntimeUnavailableError as exc:
        return {"state": "corrupt", "reason": str(exc)}
    return {"state": "ready", "reason": None}


def _discovery_error_state(exc: DiscoveryRuntimeUnavailableError) -> str:
    if isinstance(exc, DiscoveryRuntimeNotInitialized):
        return "uninitialized"
    if isinstance(exc, DiscoveryRuntimeIncomplete):
        return "incomplete"
    if isinstance(exc, DiscoveryRuntimeMaintenance):
        return "maintenance"
    return "corrupt"


@app.exception_handler(DiscoveryRuntimeUnavailableError)
async def discovery_unavailable_handler(
    request: Request, exc: DiscoveryRuntimeUnavailableError
):
    """Typed 503 for any endpoint that surfaces a discovery resolution error."""
    return JSONResponse(
        {
            "detail": {
                "state": _discovery_error_state(exc),
                "reason": str(exc),
            }
        },
        status_code=503,
    )


_PUBLIC_PATHS = {"/", "/docs", "/openapi.json", "/redoc", "/favicon.ico"}
ShortStr = Annotated[str, Field(max_length=64)]
TopicStr = Annotated[str, Field(max_length=500)]
PaperIdList = Annotated[list[str], Field(max_length=100)]
PaperNumberList = Annotated[list[str], Field(max_length=100)]


@app.middleware("http")
async def security_headers_and_api_key(request: Request, call_next):
    # Auth + security headers only.  No business service is initialized
    # here — unauthenticated requests never touch the library on disk.
    if (
        MINERU_API_KEY
        and request.method != "OPTIONS"
        and request.url.path not in _PUBLIC_PATHS
        and request.headers.get("X-API-Key") != MINERU_API_KEY
    ):
        response = JSONResponse({"detail": "invalid or missing API key"}, status_code=401)
    else:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


class PlanRequest(BaseModel):
    question: Annotated[str, Field(min_length=1, max_length=4000)]


class FulltextRequest(BaseModel):
    question: Annotated[str, Field(min_length=1, max_length=4000)]
    paper_names: PaperIdList


class CatalogEntryRequest(BaseModel):
    paper_name: ShortStr


class CreateJobRequest(BaseModel):
    topic: Annotated[str | None, Field(max_length=1000)] = None
    input_file: Annotated[str | None, Field(max_length=1000)] = None
    language: Annotated[str, Field(max_length=16)] = "zh"
    target: Annotated[str, Field(max_length=64)] = "phd_thesis"


class MatchCatalogRequest(BaseModel):
    categories: Annotated[list[TopicStr] | None, Field(max_length=20)] = None
    category_mode: Annotated[str, Field(pattern="^(union|intersection)$")] = "union"


class ConfirmPapersRequest(BaseModel):
    paper_names: PaperIdList
    confirmed_by: Annotated[str, Field(max_length=100)] = "api"


class DeepReadRequest(BaseModel):
    paper_names: Annotated[list[str] | None, Field(max_length=100)] = None
    force: bool = False


class PrepareWorksetRequest(BaseModel):
    overwrite: bool = False


class BuildStoryRequest(BaseModel):
    force: bool = False


class BuildTexRequest(BaseModel):
    title: Annotated[str | None, Field(max_length=1000)] = None
    force: bool = False
    template_only: bool = False


class CopyFiguresRequest(BaseModel):
    figures: Annotated[list[dict] | None, Field(max_length=100)] = None


class BibtexRequest(BaseModel):
    paper_numbers: PaperNumberList | None = None
    paper_names: PaperIdList | None = None


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = PROJECT_ROOT / "web" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>MinerU v2 文献资产库</h1><p>访问 /docs 查看 API。</p>")


@app.get("/catalog/all")
async def get_catalog_all():
    """List all formal papers via the 'all' category folder."""
    try:
        return {"papers": _get_catalog().list_papers(["all"])}
    except DiscoveryRuntimeUnavailableError:
        raise
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(503, str(exc))


@app.get("/catalog")
async def get_catalog_alias():
    return await get_catalog_all()


@app.get("/catalog/categories")
async def list_catalog_categories():
    """List available category folders (excluding all/_pending/system dirs)."""
    from src.catalog_folders.reader import list_categories as _list_cats
    cats = []
    for path in _list_cats(_get_catalog().root):
        try:
            data = json.loads((path / ".category.json").read_text(encoding="utf-8"))
            cats.append({
                "directory_name": path.name,
                "category_id": data.get("category_id"),
                "keyword_zh": data.get("keyword_zh"),
            })
        except Exception:
            cats.append({"directory_name": path.name, "error": "unreadable"})
    return {"categories": cats}


@app.get("/catalog/category/{name}")
async def get_catalog_category(name: str):
    """List papers in a specific category folder."""
    try:
        return {"papers": _get_catalog().list_papers([name])}
    except DiscoveryRuntimeUnavailableError:
        raise
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        raise HTTPException(503, str(exc))


@app.post("/upload")
async def upload_disabled():
    raise HTTPException(400, "direct upload is disabled; use v2 paper_raw CLI staging")


@app.get("/papers/by-number/{paper_number}")
async def get_by_number(paper_number: str):
    entry = _get_library().resolve(paper_number)
    if entry is None:
        raise HTTPException(404, f"paper_number not found: {paper_number}")
    return entry


@app.get("/papers/by-number/{paper_number}/markdown", response_class=PlainTextResponse)
async def get_markdown_by_number(paper_number: str):
    text = _get_library().read_markdown(paper_number)
    if text is None:
        raise HTTPException(404, "markdown asset not found")
    return text


@app.get("/papers/by-number/{paper_number}/images/{image_name}")
async def get_image_by_number(paper_number: str, image_name: str):
    try:
        path = _get_library().image_path(paper_number, image_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc))
    if not path.exists():
        raise HTTPException(404, "image asset not found")
    return FileResponse(path)


@app.post("/bibtex")
async def generate_bibtex(req: BibtexRequest):
    wanted_numbers = set(req.paper_numbers or [])
    wanted_ids = set(req.paper_names or [])
    if not wanted_numbers and not wanted_ids:
        raise HTTPException(400, "paper_numbers or paper_names required")
    entries = []
    lib = _get_library()
    for key in sorted(wanted_numbers|wanted_ids):
        try: row=lib.resolve(key)
        except RuntimeError as exc: raise HTTPException(503,str(exc))
        if not row: continue
        number=str(row.get("paper_number") or ""); metadata=lib.load_metadata(number)
        if not metadata: raise HTTPException(404,f"metadata asset not found: {number}")
        entries.append(bibtex_from_metadata(metadata))
    if not entries:
        raise HTTPException(404, "no matching papers")
    return {"bibtex": "\n\n".join(entries), "count": len(entries)}


@app.post("/validate/v2-library")
async def validate_v2_library_api():
    try:
        errors = _get_catalog().validate()
    except DiscoveryRuntimeUnavailableError:
        raise
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    if errors:
        raise HTTPException(503, "; ".join(errors))
    return {"valid": not errors, "errors": errors}


@app.post("/catalog/validate")
async def validate_catalog_alias():
    return await validate_v2_library_api()


@app.post("/prompt/catalog-entry")
async def prompt_catalog_entry(req: CatalogEntryRequest):
    try:
        validate_paper_name(req.paper_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    out = _get_prompt_builder().build_catalog_entry_prompt(req.paper_name)
    if not out.get("success"):
        raise HTTPException(404, out.get("error", "failed"))
    return out


@app.post("/prompt/plan-reading")
async def prompt_plan_reading(req: PlanRequest):
    if not req.question.strip():
        raise HTTPException(400, "question is required")
    out = _get_prompt_builder().build_catalog_planning_prompt(req.question.strip())
    if not out.get("success"):
        raise HTTPException(400, out.get("error", "failed"))
    return out


@app.post("/prompt/read-fulltext")
async def prompt_read_fulltext(req: FulltextRequest):
    if not req.question.strip():
        raise HTTPException(400, "question is required")
    if not req.paper_names:
        raise HTTPException(400, "paper_names cannot be empty")
    # paper_names may be 16-digit paper_number or paper_name; both pass validate_paper_name.
    try:
        for pid in req.paper_names:
            validate_paper_name(pid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    out = _get_prompt_builder().build_fulltext_prompt(req.question.strip(), req.paper_names)
    if not out.get("success"):
        raise HTTPException(400, out.get("error", "failed"))
    return out


def _check_job_id(job_id: str) -> None:
    try:
        validate_job_id(job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/write/jobs")
async def create_write_job(req: CreateJobRequest):
    if req.input_file:
        raise HTTPException(400, "input_file is not accepted by HTTP API; use CLI for local files.")
    jm = _get_job_manager()
    info = jm.create(topic=req.topic, input_file=None, target=req.target, language=req.language)
    norm = normalize_task(info["job_id"], jm)
    info["normalized_task"] = norm["normalized_path"]
    return info


@app.get("/write/jobs")
async def list_write_jobs():
    return {"jobs": _get_job_manager().list_jobs()}


@app.get("/write/jobs/{job_id}")
async def get_write_job(job_id: str):
    _check_job_id(job_id)
    meta = _get_job_manager().load_meta(job_id)
    if meta is None:
        raise HTTPException(404, f"job not found: {job_id}")
    return meta


@app.get("/write/jobs/{job_id}/files")
async def write_job_files(job_id: str):
    _check_job_id(job_id)
    if not _get_job_manager().job_dir(job_id).exists():
        raise HTTPException(404, f"job not found: {job_id}")
    return {"job_id": job_id, "files": _get_job_manager().job_files(job_id)}


@app.post("/write/jobs/{job_id}/match-catalog")
async def write_match_catalog(job_id: str, req: MatchCatalogRequest = Body(default_factory=MatchCatalogRequest)):
    _check_job_id(job_id)
    jm = _get_job_manager()
    if not jm.load_meta(job_id):
        raise HTTPException(404, f"job not found: {job_id}")
    return match_catalog(job_id, jm=jm, catalog=_get_catalog(), categories=req.categories, category_mode=req.category_mode)


@app.post("/write/jobs/{job_id}/confirm-papers")
async def write_confirm_papers(job_id: str, req: ConfirmPapersRequest):
    _check_job_id(job_id)
    if not req.paper_names:
        raise HTTPException(400, "paper_names cannot be empty")
    selected = [{"paper_name": pid, "reason": "", "expected_use": "", "priority": 3} for pid in req.paper_names]
    return confirm_selected_papers(job_id, selected, confirmed_by=req.confirmed_by, jm=_get_job_manager(), catalog=_get_catalog())


@app.post("/write/jobs/{job_id}/prepare-workset")
async def write_prepare_workset(job_id: str, req: PrepareWorksetRequest = Body(default_factory=PrepareWorksetRequest)):
    _check_job_id(job_id)
    jm = _get_job_manager()
    if not jm.load_meta(job_id):
        raise HTTPException(404, f"job not found: {job_id}")
    try:
        return prepare_workset(job_id, jm=jm, catalog=_get_catalog(), overwrite=req.overwrite, apply=True)
    except DiscoveryRuntimeUnavailableError:
        raise
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/write/jobs/{job_id}/deep-read")
async def write_deep_read(job_id: str, req: DeepReadRequest):
    _check_job_id(job_id)
    try:
        return deep_read(job_id, req.paper_names, force=req.force, jm=_get_job_manager(), catalog=_get_catalog())
    except DiscoveryRuntimeUnavailableError:
        raise
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/write/jobs/{job_id}/mark-deep-read")
async def write_mark_deep_read(job_id: str):
    _check_job_id(job_id)
    info = mark_deep_reading_filled(job_id, jm=_get_job_manager())
    if not info["filled"]:
        raise HTTPException(400, "deep reading notes invalid: " + "; ".join(info["errors"]))
    return info


@app.post("/write/jobs/{job_id}/build-story")
async def write_build_story(job_id: str, req: BuildStoryRequest = Body(default_factory=BuildStoryRequest)):
    _check_job_id(job_id)
    try:
        return build_story(job_id, force=req.force, jm=_get_job_manager(), catalog=_get_catalog())
    except DiscoveryRuntimeUnavailableError:
        raise
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/write/jobs/{job_id}/mark-story")
async def write_mark_story(job_id: str):
    _check_job_id(job_id)
    info = mark_story_filled(job_id, jm=_get_job_manager())
    if not info["filled"]:
        raise HTTPException(400, "story invalid: " + "; ".join(info["errors"]))
    return info


@app.post("/write/jobs/{job_id}/build-tex")
async def write_build_tex(job_id: str, req: BuildTexRequest):
    _check_job_id(job_id)
    try:
        return build_tex(job_id, title=req.title, force=req.force, template_only=req.template_only, jm=_get_job_manager())
    except DiscoveryRuntimeUnavailableError:
        raise
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/write/jobs/{job_id}/mark-tex")
async def write_mark_tex(job_id: str):
    _check_job_id(job_id)
    info = mark_tex_content_filled(job_id, jm=_get_job_manager())
    if not info["filled"]:
        raise HTTPException(400, "tex invalid: " + "; ".join(info["errors"]))
    return info


@app.post("/write/jobs/{job_id}/copy-figures")
async def write_copy_figures(job_id: str, req: CopyFiguresRequest):
    _check_job_id(job_id)
    return copy_figures(job_id, figures=req.figures, jm=_get_job_manager(), catalog=_get_catalog())


@app.post("/write/jobs/{job_id}/validate")
async def write_validate(job_id: str):
    _check_job_id(job_id)
    return validate_job(job_id, jm=_get_job_manager())


@app.get("/status")
async def status():
    from src.catalog_folders.formal_registry import FormalPaperRegistry
    from src.catalog_folders.validation import doctor
    from src.library.paper_number_ledger import PaperNumberLedger
    from config.settings import PAPER_NUMBER_LEDGER_PATH
    try:
        reg = FormalPaperRegistry(papers_dir=PAPERS_DIR, ledger=PaperNumberLedger(PAPER_NUMBER_LEDGER_PATH))
        d = doctor(root=CATALOG_FOLDER_ROOT, formal_registry=reg)
        count = d["active_formal_papers"]
        category_state = "ready" if d["writer_category_safe"] else "dirty_or_invalid" if d["dirty"] else "incomplete"
    except Exception:
        count = 0; category_state = "error"
    discovery = _discovery_state()
    return {
        "status": "running",
        "version": "4.0.0",
        "mode": "pure_v2_paper_raw",
        "mineru_backend": "hybrid-engine",
        "formal_papers": count,
        "category_state": category_state,
        "discovery": discovery["state"],
        "discovery_reason": discovery["reason"],
    }


@app.get("/status/discovery")
async def status_discovery():
    """Discovery runtime state, with typed 503 for non-ready states.

    - 200 ``{"discovery": "ready"}`` — an active v4 generation resolves.
    - 503 ``detail.state == "uninitialized"`` — fresh install, bootstrap
      has not run; this is the ONLY degradable state.
    - 503 ``detail.state == "corrupt" | "incomplete" | "maintenance"`` —
      damaged or blocked production state; fail closed.
    """
    discovery = _discovery_state()
    if discovery["state"] == "ready":
        return {"discovery": "ready"}
    return JSONResponse(
        {
            "detail": {
                "state": discovery["state"],
                "reason": discovery["reason"],
            }
        },
        status_code=503,
    )


@app.get("/status/runtime")
async def status_runtime():
    from src.mineru.converter import MINERU_EXE
    from src.mineru.lock import read_mineru_lock_status
    from src.mineru.runtime import (
        describe_runtime,
        preflight_gpu,
        preflight_mineru_api,
        preflight_mineru_cli,
        preflight_torch_cuda,
        runtime_config_from_env,
    )

    config = runtime_config_from_env()
    return {
        "runtime": describe_runtime(config),
        "gpu": preflight_gpu().__dict__,
        "torch_cuda": preflight_torch_cuda().__dict__,
        "cli": preflight_mineru_cli(MINERU_EXE).__dict__,
        "api": preflight_mineru_api(config.api_url).__dict__,
        "mineru_lock": read_mineru_lock_status(),
    }


if __name__ == "__main__":
    from config.settings import ensure_runtime_dirs, validate_settings
    from src.utils.logging_setup import configure_logging

    validate_settings()
    ensure_runtime_dirs()
    configure_logging()
    uvicorn.run("src.server:app", host=API_HOST, port=API_PORT, reload=False, log_level="info")
