@echo off
chcp 65001 >nul
echo ========================================
echo   MinerU 文献资产库 v3.4
echo   Web服务端口: 8080
echo   默认 Runner: CLI
echo   Persistent mineru-api: use scripts\start_mineru_services.py --wait
echo ========================================
echo.

cd /d "%~dp0"

:: MinerU runtime. HTTP mineru-api upload adapter is not enabled by default.
set CUDA_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6
set PATH=%CUDA_PATH%\bin;%PATH%
set MINERU_RUNNER=cli
set MINERU_REQUIRE_GPU=true
set MINERU_BACKEND=hybrid-engine
set MINERU_EFFORT=medium
set MINERU_METHOD=auto

call conda activate mineru
if %errorlevel% neq 0 (
    echo [!] 请先安装 Miniconda 并创建 mineru 环境
    pause
    exit /b 1
)

echo 启动文献库 Web 服务 (端口8080)...
echo      访问: http://localhost:8080
echo      API文档: http://localhost:8080/docs
echo      Runtime: http://localhost:8080/status/runtime
echo.
python -m src.server

pause
