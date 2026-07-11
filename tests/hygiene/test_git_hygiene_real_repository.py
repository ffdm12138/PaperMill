import subprocess
from pathlib import Path

from scripts.agent_acceptance import verify_git_hygiene


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_git_hygiene_rejects_all_tracked_runtime_members(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    forbidden = [
        ".workbuddy/memory/x.md", ".reasonix/x.md", "data/cleanup_report.json",
        "data/paper_raw/0000000000000001/paper.pdf",
    ]
    allowed = ["data/paper_raw/.gitkeep", "data/papers/.gitkeep", "data/transactions/.gitkeep"]
    for rel in forbidden + allowed:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
        _git(repo, "add", "-f", rel)
    errors = verify_git_hygiene(repo)
    assert len(errors) == len(forbidden)
    assert all(any(rel in error for error in errors) for rel in forbidden)
    assert not any(any(rel in error for rel in allowed) for error in errors)
