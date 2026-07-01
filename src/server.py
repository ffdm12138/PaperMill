"""FastAPI service for the pure v2 paper_raw library."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import API_CORS_ORIGINS, API_HOST, API_PORT, ALL_CATALOG_PATH, MINERU_API_KEY
from src.catalog import Catalog
from src.library import PaperLibrary
from src.naming import validate_job_id, validate_paper_id
from src.prompt_builder import PromptBuilder
from src.services.v2_library import AllCatalogBuilder, bibtex_from_metadata
from src.writer.bib_manager import portability_check, validate_catalog_citations, validate_job_citations
from src.writer.catalog_matcher import confirm_selected_papers, match_catalog
from src.writer.deep_reader import deep_read, mark_deep_reading_filled, prepare_workset
from src.writer.figure_manager import copy_figures
from src.writer.job_manager import JobManager
from src.writer.job_validator import validate_job
from src.writer.story_builder import build_story, mark_story_filled
from src.writer.tex_project import build_tex, mark_tex_content_filled
from src.writer.topic_parser import normalize_task


app = FastAPI(
    title="MinerU v2 文献资产库",
    description="paper_raw 入库 + all.catalog 读取 + 按需全文复制",
    version="4.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=API_CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)

catalog = Catalog()
library = PaperLibrary(catalog=catalog)
prompt_builder = PromptBuilder(catalog=catalog, library=library)
job_manager = JobManager()


_PUBLIC_PATHS = {"/", "/docs", "/openapi.json", "/redoc", "/favicon.ico"}
ShortStr = Annotated[str, Field(max_length=64)]
TopicStr = Annotated[str, Field(max_length=500)]
PaperIdList = Annotated[list[str], Field(max_length=100)]
PaperNumberList = Annotated[list[str], Field(max_length=100)]


@app.middleware("http")
async def security_headers_and_api_key(request: Request, call_next):
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
    paper_ids: PaperIdList


class CatalogEntryRequest(BaseModel):
    paper_id: ShortStr


class CreateJobRequest(BaseModel):
    topic: Annotated[str | None, Field(max_length=1000)] = None
    input_file: Annotated[str | None, Field(max_length=1000)] = None
    language: Annotated[str, Field(max_length=16)] = "zh"
    target: Annotated[str, Field(max_length=64)] = "phd_thesis"


class MatchCatalogRequest(BaseModel):
    topics: Annotated[list[TopicStr] | None, Field(max_length=20)] = None


class ConfirmPapersRequest(BaseModel):
    paper_ids: PaperIdList
    confirmed_by: Annotated[str, Field(max_length=100)] = "api"


class DeepReadRequest(BaseModel):
    paper_ids: Annotated[list[str] | None, Field(max_length=100)] = None
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
    paper_ids: PaperIdList | None = None


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = PROJECT_ROOT / "web" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>MinerU v2 文献资产库</h1><p>访问 /docs 查看 API。</p>")


@app.get("/catalog/all")
async def get_all_catalog(rebuild: bool = False):
    # Only an explicit ?rebuild=true writes to disk. A read on a missing
    # all.catalog returns an in-memory snapshot without creating the file.
    if rebuild:
        return AllCatalogBuilder().build(write=True)
    if not ALL_CATALOG_PATH.exists():
        return AllCatalogBuilder().build(write=False)
    return json.loads(ALL_CATALOG_PATH.read_text(encoding="utf-8"))


@app.get("/catalog")
async def get_catalog_alias():
    return await get_all_catalog(rebuild=False)


@app.post("/upload")
async def upload_disabled():
    raise HTTPException(400, "direct upload is disabled; use v2 paper_raw CLI staging")


@app.get("/papers/by-number/{paper_number}")
async def get_by_number(paper_number: str):
    entry = library.resolve(paper_number)
    if entry is None:
        raise HTTPException(404, f"paper_number not found: {paper_number}")
    return entry


@app.get("/papers/by-number/{paper_number}/markdown", response_class=PlainTextResponse)
async def get_markdown_by_number(paper_number: str):
    text = library.read_markdown(paper_number)
    if text is None:
        raise HTTPException(404, "markdown asset not found")
    return text


@app.get("/papers/by-number/{paper_number}/images/{image_name}")
async def get_image_by_number(paper_number: str, image_name: str):
    try:
        path = library.image_path(paper_number, image_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(404, str(exc))
    if not path.exists():
        raise HTTPException(404, "image asset not found")
    return FileResponse(path)


@app.post("/bibtex")
async def generate_bibtex(req: BibtexRequest):
    # Read-only: build an in-memory snapshot if all.catalog is missing; do not
    # write to disk from a bibtex request.
    if not ALL_CATALOG_PATH.exists():
        data = AllCatalogBuilder().build(write=False)
    else:
        data = json.loads(ALL_CATALOG_PATH.read_text(encoding="utf-8"))
    wanted_numbers = set(req.paper_numbers or [])
    wanted_ids = set(req.paper_ids or [])
    if not wanted_numbers and not wanted_ids:
        raise HTTPException(400, "paper_numbers or paper_ids required")
    entries = []
    for item in data.get("papers", []):
        if item.get("paper_number") in wanted_numbers or item.get("paper_id") in wanted_ids:
            paper_key = item.get("paper_number") or item.get("paper_id")
            metadata = library.load_metadata(paper_key)
            if not metadata:
                raise HTTPException(404, f"metadata asset not found: {paper_key}")
            entries.append(bibtex_from_metadata(metadata, key=item.get("paper_id")))
    if not entries:
        raise HTTPException(404, "no matching papers")
    return {"bibtex": "\n\n".join(entries), "count": len(entries)}


@app.post("/validate/v2-library")
async def validate_v2_library_api():
    errors = catalog.validate()
    return {"valid": not errors, "errors": errors}


@app.post("/catalog/validate")
async def validate_catalog_alias():
    return await validate_v2_library_api()


@app.post("/prompt/catalog-entry")
async def prompt_catalog_entry(req: CatalogEntryRequest):
    try:
        validate_paper_id(req.paper_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    out = prompt_builder.build_catalog_entry_prompt(req.paper_id)
    if not out.get("success"):
        raise HTTPException(404, out.get("error", "failed"))
    return out


@app.post("/prompt/plan-reading")
async def prompt_plan_reading(req: PlanRequest):
    if not req.question.strip():
        raise HTTPException(400, "question is required")
    out = prompt_builder.build_catalog_planning_prompt(req.question.strip())
    if not out.get("success"):
        raise HTTPException(400, out.get("error", "failed"))
    return out


@app.post("/prompt/read-fulltext")
async def prompt_read_fulltext(req: FulltextRequest):
    if not req.question.strip():
        raise HTTPException(400, "question is required")
    # paper_ids may be 16-digit paper_number or paper_id; both pass validate_paper_id.
    try:
        for pid in req.paper_ids:
            validate_paper_id(pid)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    out = prompt_builder.build_fulltext_prompt(req.question.strip(), req.paper_ids)
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
    info = job_manager.create(topic=req.topic, input_file=None, target=req.target, language=req.language)
    norm = normalize_task(info["job_id"], job_manager)
    info["normalized_task"] = norm["normalized_path"]
    return info


@app.get("/write/jobs")
async def list_write_jobs():
    return {"jobs": job_manager.list_jobs()}


@app.get("/write/jobs/{job_id}")
async def get_write_job(job_id: str):
    _check_job_id(job_id)
    meta = job_manager.load_meta(job_id)
    if meta is None:
        raise HTTPException(404, f"job not found: {job_id}")
    return meta


@app.get("/write/jobs/{job_id}/files")
async def write_job_files(job_id: str):
    _check_job_id(job_id)
    if not job_manager.job_dir(job_id).exists():
        raise HTTPException(404, f"job not found: {job_id}")
    return {"job_id": job_id, "files": job_manager.job_files(job_id)}


@app.post("/write/jobs/{job_id}/match-catalog")
async def write_match_catalog(job_id: str, req: MatchCatalogRequest = Body(default_factory=MatchCatalogRequest)):
    _check_job_id(job_id)
    if not job_manager.load_meta(job_id):
        raise HTTPException(404, f"job not found: {job_id}")
    return match_catalog(job_id, jm=job_manager, catalog=catalog, topics=req.topics)


@app.post("/write/jobs/{job_id}/confirm-papers")
async def write_confirm_papers(job_id: str, req: ConfirmPapersRequest):
    _check_job_id(job_id)
    if not req.paper_ids:
        raise HTTPException(400, "paper_ids cannot be empty")
    selected = [{"paper_id": pid, "reason": "", "expected_use": "", "priority": 3} for pid in req.paper_ids]
    return confirm_selected_papers(job_id, selected, confirmed_by=req.confirmed_by, jm=job_manager, catalog=catalog)


@app.post("/write/jobs/{job_id}/prepare-workset")
async def write_prepare_workset(job_id: str, req: PrepareWorksetRequest = Body(default_factory=PrepareWorksetRequest)):
    _check_job_id(job_id)
    if not job_manager.load_meta(job_id):
        raise HTTPException(404, f"job not found: {job_id}")
    try:
        return prepare_workset(job_id, jm=job_manager, catalog=catalog, overwrite=req.overwrite, apply=True)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/write/jobs/{job_id}/deep-read")
async def write_deep_read(job_id: str, req: DeepReadRequest):
    _check_job_id(job_id)
    try:
        return deep_read(job_id, req.paper_ids, force=req.force, jm=job_manager, catalog=catalog)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/write/jobs/{job_id}/mark-deep-read")
async def write_mark_deep_read(job_id: str):
    _check_job_id(job_id)
    info = mark_deep_reading_filled(job_id, jm=job_manager)
    if not info["filled"]:
        raise HTTPException(400, "deep reading notes invalid: " + "; ".join(info["errors"]))
    return info


@app.post("/write/jobs/{job_id}/build-story")
async def write_build_story(job_id: str, req: BuildStoryRequest = Body(default_factory=BuildStoryRequest)):
    _check_job_id(job_id)
    try:
        return build_story(job_id, force=req.force, jm=job_manager, catalog=catalog)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/write/jobs/{job_id}/mark-story")
async def write_mark_story(job_id: str):
    _check_job_id(job_id)
    info = mark_story_filled(job_id, jm=job_manager)
    if not info["filled"]:
        raise HTTPException(400, "story invalid: " + "; ".join(info["errors"]))
    return info


@app.post("/write/jobs/{job_id}/build-tex")
async def write_build_tex(job_id: str, req: BuildTexRequest):
    _check_job_id(job_id)
    try:
        return build_tex(job_id, title=req.title, force=req.force, template_only=req.template_only, jm=job_manager)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))


@app.post("/write/jobs/{job_id}/mark-tex")
async def write_mark_tex(job_id: str):
    _check_job_id(job_id)
    info = mark_tex_content_filled(job_id, jm=job_manager)
    if not info["filled"]:
        raise HTTPException(400, "tex invalid: " + "; ".join(info["errors"]))
    return info


@app.post("/write/jobs/{job_id}/copy-figures")
async def write_copy_figures(job_id: str, req: CopyFiguresRequest):
    _check_job_id(job_id)
    return copy_figures(job_id, figures=req.figures, jm=job_manager, catalog=catalog)


@app.post("/write/jobs/{job_id}/validate")
async def write_validate(job_id: str):
    _check_job_id(job_id)
    return validate_job(job_id, jm=job_manager)


@app.get("/status")
async def status():
    return {
        "status": "running",
        "version": "4.0.0",
        "mode": "pure_v2_paper_raw",
        "mineru_backend": "hybrid-engine",
        "all_catalog_papers": len(catalog.list_papers()),
    }


@app.get("/status/runtime")
async def status_runtime():
    from src.converter import MINERU_EXE
    from src.mineru_lock import read_mineru_lock_status
    from src.mineru_runtime import (
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
    uvicorn.run("src.server:app", host=API_HOST, port=API_PORT, reload=False, log_level="info")
