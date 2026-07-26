"""Phase 2 验收：converter 所有返回分支都带 backend/method/effort/runner。"""
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch, MagicMock
from src.mineru.converter import MinerUConverter


_EXPECTED = {"backend": "hybrid-engine", "method": "auto",
             "effort": "medium", "runner": "cli"}


@contextmanager
def _mock_converter_deps(subprocess_result=None):
    """统一 mock converter 的 subprocess / lock / snapshot / preflight。

    subprocess_result: MagicMock 作为 subprocess.run() 的返回值。
                       若为 callable，作为 side_effect 使用。
    """
    with patch("src.mineru.converter.preflight_gpu",
               return_value=type("H", (), {"ok": True, "message": "ok", "nvidia_smi": True})()):
        with patch("src.mineru.converter.preflight_torch_cuda",
                   return_value=type("T", (), {"ok": True, "message": "ok"})()):
            with patch("src.mineru.converter.snapshot_nvidia_smi",
                       return_value={"available": False}):
                with patch("src.mineru.converter.MinerULock.acquire",
                           return_value=True):
                    with patch("src.mineru.converter.MinerULock.release"):
                        if subprocess_result is not None:
                            mock_run = MagicMock()
                            if callable(subprocess_result) and not isinstance(subprocess_result, MagicMock):
                                mock_run.side_effect = subprocess_result
                            else:
                                mock_run.return_value = subprocess_result
                            with patch("src.mineru.converter.subprocess.run", mock_run):
                                yield mock_run
                        else:
                            yield None


def _make_output(out_dir: Path, stem: str, content: str = "# md"):
    """构造 mineru CLI 成功后的输出结构: out_dir/stem/hybrid_auto/stem.md"""
    md = out_dir / stem / "hybrid_auto" / f"{stem}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(content, encoding="utf-8")


def test_success_returns_all_fields():
    """成功返回含 backend/method/effort/runner。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.pdf"
        src.write_bytes(b"%PDF-1.4 fake")
        out = Path(td) / "out"
        _make_output(out, "in")

        conv = MinerUConverter(log_dir="")
        with _mock_converter_deps(
            subprocess_result=MagicMock(returncode=0, stdout="", stderr="")
        ):
            result = conv.convert_via_cli(src, out, backend="hybrid-engine",
                                          method="auto", effort="medium")
        assert result["success"] is True
        for k, v in _EXPECTED.items():
            assert result[k] == v, f"{k} expected {v}, got {result.get(k)}"


def test_file_not_found_returns_all_fields():
    """文件不存在时返回含全部字段。"""
    conv = MinerUConverter(log_dir="")
    with _mock_converter_deps():
        result = conv.convert_via_cli("/nonexistent/x.pdf", "/tmp/out",
                                      backend="hybrid-engine", method="auto",
                                      effort="medium")
    assert result["success"] is False
    for k, v in _EXPECTED.items():
        assert result[k] == v


def test_subprocess_failure_returns_all_fields():
    """subprocess 非零退出时返回含全部字段。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.pdf"
        src.write_bytes(b"%PDF-1.4 fake")
        conv = MinerUConverter(log_dir="")
        with _mock_converter_deps(
            subprocess_result=MagicMock(returncode=1, stdout="", stderr="boom")
        ):
            result = conv.convert_via_cli(src, Path(td) / "out", backend="hybrid-engine",
                                          method="auto", effort="medium")
        assert result["success"] is False
        assert "boom" in result["error"]
        for k, v in _EXPECTED.items():
            assert result[k] == v


def test_subprocess_run_has_no_conversion_timeout():
    """MinerU conversion subprocess is intentionally unbounded."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.pdf"
        src.write_bytes(b"%PDF-1.4 fake")
        out = Path(td) / "out"
        _make_output(out, "in")
        conv = MinerUConverter(log_dir="")
        with _mock_converter_deps(
            subprocess_result=MagicMock(returncode=0, stdout="", stderr="")
        ) as mock_run:
            result = conv.convert_via_cli(src, out, backend="hybrid-engine",
                                          method="auto", effort="medium")
        assert result["success"] is True
        assert mock_run is not None
        assert "timeout" not in mock_run.call_args.kwargs


def test_no_legacy_backend_key():
    """返回值不得再用 backend 表示 cli/api（那是 runner 语义）。"""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.pdf"
        src.write_bytes(b"%PDF-1.4 fake")
        conv = MinerUConverter(log_dir="")
        with _mock_converter_deps(
            subprocess_result=MagicMock(returncode=0, stdout="", stderr="")
        ):
            _make_output(Path(td) / "out", "in")
            result = conv.convert_via_cli(src, Path(td) / "out", backend="hybrid-engine",
                                          method="auto", effort="medium")
        # backend 必须是 MinerU 后端，不是 cli/api
        assert result["backend"] == "hybrid-engine"
        assert result["runner"] == "cli"
