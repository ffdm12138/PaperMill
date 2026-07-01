"""External command plugin resolver。

允许用户配置外部命令作为 PDF 获取插件。默认禁用，须显式启用。
命令输出必须是 JSON，至少包含 ``success``、``pdf_path/pdf_url``、``source``、``message``。
"""
import json
import re
import subprocess
from pathlib import Path

from loguru import logger

from ..models import FetchResult
from .base import PdfResolver, ResolveContext


_DOI_RE = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Z0-9]+$", re.IGNORECASE)
_FORBIDDEN_DOI_CHARS = re.compile(r'[\s\x00-\x1f\x7f<>"\\]')


def _valid_custom_resolver_doi(doi: str) -> bool:
    return bool(doi and _DOI_RE.match(doi) and not _FORBIDDEN_DOI_CHARS.search(doi))


class ExternalCommandResolver(PdfResolver):
    name = "custom"
    access_modes = ("custom",)

    def __init__(self, command_argv: list[str] | tuple[str, ...] | None = None):
        self.command_argv = list(command_argv or [])

    def resolve(self, context: ResolveContext) -> FetchResult:
        if not self.command_argv:
            return FetchResult(
                doi=context.doi, source="custom", error="custom resolver command_argv is not configured",
            )
        doi_raw = context.doi or ""
        doi = doi_raw.strip()
        if doi_raw != doi:
            return FetchResult(
                doi=context.doi, source="custom", error="invalid DOI format for custom resolver",
            )
        if not _valid_custom_resolver_doi(doi):
            return FetchResult(
                doi=context.doi, source="custom", error="invalid DOI format for custom resolver",
            )
        if "{doi}" in self.command_argv[0]:
            return FetchResult(
                doi=context.doi,
                source="custom",
                error="custom resolver executable cannot contain {doi}",
            )
        allowed_dir = Path(
            (context.metadata or {}).get("allowed_output_dir")
            or getattr(context.access_policy, "extra", {}).get("allowed_output_dir", "")
            or Path.cwd()
        )
        args = [
            part.replace("{doi}", doi).replace("{output_dir}", str(allowed_dir))
            for part in self.command_argv
        ]
        try:
            result = subprocess.run(
                args, shell=False, capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                return FetchResult(
                    doi=context.doi, source="custom", error=result.stderr.strip(),
                )
            output = json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            return FetchResult(
                doi=context.doi, source="custom", error="command timed out",
            )
        except json.JSONDecodeError as e:
            return FetchResult(
                doi=context.doi, source="custom", error=f"invalid JSON output: {e}",
            )
        except Exception as e:
            return FetchResult(
                doi=context.doi, source="custom", error=str(e),
            )

        pdf_path = output.get("pdf_path", "")
        pdf_url = output.get("pdf_url", "")
        if pdf_path:
            error = _validate_pdf_path(Path(pdf_path), allowed_dir)
            if error:
                return FetchResult(doi=context.doi, source="custom", error=error)
        return FetchResult(
            doi=context.doi,
            success=bool(pdf_path or pdf_url),
            source="custom",
            resolver=self.name,
            access_mode="custom",
            access_status="custom",
            pdf_url=pdf_url,
            output_path=pdf_path,
            metadata={"command_argv": args, "raw_output": output},
        )


def _validate_pdf_path(path: Path, allowed_dir: Path) -> str:
    try:
        resolved = path.resolve()
        allowed = allowed_dir.resolve()
        resolved.relative_to(allowed)
    except (OSError, ValueError):
        return f"resolver output path is outside allowed directory: {path}"
    if resolved.suffix.lower() != ".pdf":
        return f"resolver output is not a .pdf file: {path}"
    if not resolved.exists():
        return f"resolver output PDF does not exist: {path}"
    if resolved.stat().st_size <= 0:
        return f"resolver output PDF is empty: {path}"
    with resolved.open("rb") as fh:
        if fh.read(5) != b"%PDF-":
            return f"resolver output is not a valid PDF: {path}"
    return ""
