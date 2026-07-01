# MinerU Performance Notes

Use one MinerU conversion at a time by default. Hybrid engine may use several GB of GPU memory per process.
MinerU conversion requires GPU / MinerU 正式转换必须使用 GPU by default.
Formal ingest should use `scripts/convert_paper_raw_gpu.py`, which defaults
`MINERU_REQUIRE_GPU=true`, `CUDA_VISIBLE_DEVICES=0`, and verifies
`torch.cuda.is_available()` before conversion.
CPU/no-GPU conversion is debug-only via `MINERU_ALLOW_CPU=true` or explicit
`MINERU_REQUIRE_GPU=false`.

## Diagnostics

```bash
python scripts/start_mineru_services.py --wait
python scripts/check_mineru_processes.py
python scripts/benchmark_mineru.py "E:\papers\test.pdf" --repeat 2
python scripts/convert_paper_raw_gpu.py --source-id 000001 --apply
python scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
python scripts/stop_mineru_services.py
```

For faster repeated conversion, start persistent `mineru-api` first. On Windows,
`python scripts/start_mineru_services.py --wait` is the recommended helper and
`start_fast_api_mode.bat` delegates to it. Otherwise start `mineru-api`
according to the local MinerU installation. Then set:

```bash
set MINERU_REQUIRE_GPU=true
set CUDA_VISIBLE_DEVICES=0
set MINERU_RUNNER=cli_api_proxy
set MINERU_API_URL=http://127.0.0.1:8000
```

PowerShell:

```powershell
$env:MINERU_REQUIRE_GPU="true"
$env:CUDA_VISIBLE_DEVICES="0"
$env:MINERU_RUNNER="cli_api_proxy"
$env:MINERU_API_URL="http://127.0.0.1:8000"
```

Linux / bash:

```bash
export MINERU_REQUIRE_GPU=true
export CUDA_VISIBLE_DEVICES=0
export MINERU_RUNNER=cli_api_proxy
export MINERU_API_URL=http://127.0.0.1:8000
```

`mineru-api` must be started with `CUDA_VISIBLE_DEVICES=0` in its own shell.
Setting `CUDA_VISIBLE_DEVICES` only in the client process cannot change an
already-running `mineru-api` process.

`start_fast_api_mode.bat` is a single-instance helper: it first checks
`http://127.0.0.1:8000/health`, reuses an existing healthy `mineru-api`, and
refuses to start a new API when port 8000 is occupied but health is unavailable.
If that happens, run:

```bash
python scripts/check_mineru_processes.py
python scripts/stop_mineru_services.py
python scripts/stop_mineru_services.py --all-mineru-api
```

MinerU PDF conversion has no process-level timeout. Large PDFs may run for a
long time and are not killed after a fixed second count. Health checks,
preflight checks, HTTP request timeouts, and `MinerULock` wait timeouts are
separate protections and do not imply a PDF conversion timeout.

Metadata title/author/affiliation/abstract/keyword/DOI candidates should be read
from the converted Markdown first 100 lines as front-matter evidence before any
PDF title fallback.

Formal multi-source conversion must not use `MINERU_RUNNER=cli`, because that
can cold-start MinerU once per PDF. Use `cli_api_proxy`; single-source CLI is
kept only for tests/debugging. `paper_raw` conversion is idempotent: existing
`<source_id>.md` plus `images/` is skipped by default, successful conversion
writes `<source_id>.conversion.json` and `.import_status.json: converted`, and
stale/partial conversion states require explicit `--force-reconvert`.
