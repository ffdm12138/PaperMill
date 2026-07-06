import argparse
import json
from pathlib import Path

import pytest

from scripts.check_write_quality_text import check_write_quality_text, main


def _write_job(tmp_path: Path, *, sections: dict[str, str], bib_keys: list[str] | None = None) -> Path:
    write_dir = tmp_path / "write" / "jobs"
    tex_dir = write_dir / "job_quality" / "tex"
    sections_dir = tex_dir / "sections"
    sections_dir.mkdir(parents=True)
    bib_keys = bib_keys or ["key1", "key2", "key3"]
    (tex_dir / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "\\input{sections/introduction}\n"
        "\\input{sections/body}\n"
        "\\input{sections/conclusion}\n"
        "\\bibliography{references}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    for name, text in sections.items():
        (sections_dir / name).write_text(text, encoding="utf-8")
    bib_text = "\n\n".join(
        f"@article{{{key},\n  title = {{{key}}},\n  doi = {{10.1234/{key}}},\n}}"
        for key in bib_keys
    )
    (tex_dir / "references.bib").write_text(bib_text, encoding="utf-8")
    return write_dir


def _pass_sections() -> dict[str, str]:
    return {
        "introduction.tex": (
            "\\section{Introduction}\n"
            "This review follows a mechanism chain from threshold to sublimation \\cite{key1}.\n"
        ),
        "body.tex": (
            "\\section{Mechanisms}\n"
            "The study object, method/data, key conclusion, and review role are synthesized "
            "across papers \\cite{key2,key3}.\n"
            "\\section{Uncertainty}\n"
            "The main uncertainty and limitations come from particle-size evolution.\n"
        ),
        "conclusion.tex": "\\section{Conclusion}\nThe mechanism chain is complete.\n",
    }


def _run(write_dir: Path) -> dict:
    return check_write_quality_text(argparse.Namespace(job_id="job_quality", write_dir=write_dir))


def test_quality_check_passes_for_complete_article(tmp_path):
    write_dir = _write_job(tmp_path, sections=_pass_sections())

    report = _run(write_dir)

    assert report["valid"] is True
    assert report["errors"] == []
    assert report["bib_count"] == 3
    assert report["citation_count"] == 3
    assert (write_dir / "job_quality" / "reports" / "write_quality_check_report.json").exists()


def test_quality_check_rejects_points_out_template(tmp_path):
    sections = _pass_sections()
    sections["body.tex"] += "\nClifton指出：Clifton指出了一个模板句。\\cite{key1}\n"
    write_dir = _write_job(tmp_path, sections=sections)

    report = _run(write_dir)

    assert report["valid"] is False
    assert any("template" in error for error in report["errors"])


@pytest.mark.parametrize("placeholder", ["smoke", "闭环引用演示", "本文档由 MinerU"])
def test_quality_check_rejects_placeholder_text(tmp_path, placeholder):
    sections = _pass_sections()
    sections["introduction.tex"] += f"\n{placeholder}\n"
    write_dir = _write_job(tmp_path, sections=sections)

    report = _run(write_dir)

    assert report["valid"] is False
    assert any("placeholder" in error for error in report["errors"])


def test_quality_check_rejects_uncited_bib_key(tmp_path):
    sections = _pass_sections()
    sections["body.tex"] = sections["body.tex"].replace(",key3", "")
    write_dir = _write_job(tmp_path, sections=sections)

    report = _run(write_dir)

    assert report["valid"] is False
    assert report["missing_bib_citations"] == ["key3"]
    assert any("not cited" in error for error in report["errors"])


def test_quality_check_rejects_missing_introduction_or_conclusion(tmp_path):
    sections = _pass_sections()
    sections.pop("introduction.tex")
    write_dir = _write_job(tmp_path, sections=sections)

    report = _run(write_dir)

    assert report["valid"] is False
    assert any("introduction" in error for error in report["errors"])


def test_quality_check_rejects_missing_uncertainty_or_limitation(tmp_path):
    sections = _pass_sections()
    sections["body.tex"] = (
        "\\section{Mechanisms}\n"
        "The synthesis uses all selected evidence in a mechanism chain \\cite{key2,key3}.\n"
    )
    write_dir = _write_job(tmp_path, sections=sections)

    report = _run(write_dir)

    assert report["valid"] is False
    assert any("uncertainty" in error for error in report["errors"])


def test_quality_check_cli_writes_report_and_returns_nonzero_on_failure(tmp_path):
    sections = _pass_sections()
    sections["body.tex"] += "\nTEMPLATE_ONLY\n"
    write_dir = _write_job(tmp_path, sections=sections)

    code = main(["--job-id", "job_quality", "--write-dir", str(write_dir)])
    report_path = write_dir / "job_quality" / "reports" / "write_quality_check_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert code == 1
    assert report["valid"] is False
    assert any("placeholder" in error for error in report["errors"])
