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
python scripts/start_mineru_services.py --wait --restart-if-stale
python scripts/check_mineru_processes.py
python scripts/benchmark_mineru.py "E:\papers\test.pdf" --repeat 2
python scripts/convert_paper_raw_gpu.py --paper-number 0000000000000001 --apply
python scripts/smoke_mineru_conversion.py --paper-number 0000000000000001 --apply --report reports/smoke_mineru_conversion.json
python scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
python scripts/stop_mineru_services.py
```

## Launching MinerU services

Recommended (conda on PATH):

```bash
conda run -n mineru python scripts/start_mineru_services.py --wait --restart-if-stale
conda run -n mineru python scripts/check_mineru_processes.py
conda run -n mineru python scripts/smoke_mineru_conversion.py --paper-number 0000000000000001 --apply --report reports/smoke_mineru_conversion.json
conda run -n mineru python scripts/convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
conda run -n mineru python scripts/stop_mineru_services.py
```

如果 conda 不在 PATH，用 env python 绝对路径：

```bash
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\start_mineru_services.py --wait --restart-if-stale
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\check_mineru_processes.py
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\smoke_mineru_conversion.py --paper-number 0000000000000001 --apply --report reports\smoke_mineru_conversion.json
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\convert_paper_raw_gpu.py --all --apply --report reports/convert_paper_raw.json
C:\Users\Admin\.conda\envs\mineru\python.exe scripts\stop_mineru_services.py
```

start_mineru_services.py must resolve Scripts/mineru-api.exe from the current Python env (find_mineru_api_exe). Do not manually background mineru-api.exe as a long-term SOP.

For faster repeated conversion, start persistent `mineru-api` first. On Windows,
`python scripts/start_mineru_services.py --wait --restart-if-stale` is the recommended helper and
`start_fast_api_mode.bat` delegates to it. Otherwise start `mineru-api`
according to the local MinerU installation. Then set:

```bat
:: Windows cmd.exe only
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

`/health` is liveness only, not GPU conversion readiness. Formal batch conversion
requires managed service identity, `check_mineru_processes.py` verdict
`READY_FOR_CONVERSION`, and a recent successful single-paper
`smoke_mineru_conversion.py` report.
`start_fast_api_mode.bat` is a single-instance helper: it reuses only a managed
healthy `mineru-api`, restarts stale/unmanaged pid-file services with
`--restart-if-stale`, and refuses to start a new API when port 8000 is occupied
but health is unavailable.
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
`<paper_number>.md` plus `images/` is skipped by default, successful conversion
writes `<paper_number>.conversion.json` and `.import_status.json: converted`, and
stale/partial conversion states require explicit `--force-reconvert`.
Before running MinerU, conversion checks the local raw-output cache at
`output/mineru_cache/`. A cache hit requires PDF md5 + sha256 + file size and
`backend/method/lang/effort` to match, restores md/images/manifests without GPU
preflight or `MinerULock`, and never enters git/snapshot. `--force-reconvert`
bypasses the cache, `--ignore-output-cache` disables lookup, and `--cache-only`
restores verified cache hits without running MinerU.
