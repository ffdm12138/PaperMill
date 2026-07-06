@echo off
chcp 65001 >nul
echo ========================================
echo   MinerU PaperMill - fast API mode
echo   Runner: cli_api_proxy (CLI + --api-url)
echo   Uses scripts\start_mineru_services.py for single-instance startup
echo ========================================
echo.

cd /d "%~dp0"

set CUDA_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6
set CUDA_VISIBLE_DEVICES=0
set PATH=%CUDA_PATH%\bin;%PATH%
set MINERU_RUNNER=cli_api_proxy
set MINERU_REQUIRE_GPU=true
set MINERU_API_URL=http://127.0.0.1:8000
set MINERU_BACKEND=hybrid-engine
set MINERU_EFFORT=medium
set MINERU_METHOD=auto

call conda activate mineru
if %errorlevel% neq 0 (
    echo [!] Please install Miniconda and create the mineru environment first.
    pause
    exit /b 1
)

echo [1/2] Start or restart stale mineru-api...
python scripts\start_mineru_services.py --wait --restart-if-stale --port 8000 --api-url http://127.0.0.1:8000 --cuda-visible-devices 0 --cuda-path "%CUDA_PATH%"
if %errorlevel% neq 0 (
    echo [!] mineru-api startup failed. Check data\logs\mineru_api.log.
    pause
    exit /b 1
)

echo.
echo [2/2] Starting PaperMill web service on port 8080...
echo      URL: http://localhost:8080
echo      API docs: http://localhost:8080/docs
echo      Runtime: http://localhost:8080/status/runtime
echo.
python -m src.server

pause
