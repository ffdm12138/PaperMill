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

:: Watcher 默认关闭，避免后台抢 GPU 影响批量导入或 benchmark。
:: 需要 watcher 时手动: set START_WATCHER=1 && start.bat
:: 或者直接运行: python watcher.py --interval 30
if "%START_WATCHER%"=="" set START_WATCHER=0

call conda activate mineru
if %errorlevel% neq 0 (
    echo [!] 请先安装 Miniconda 并创建 mineru 环境
    pause
    exit /b 1
)

if "%START_WATCHER%"=="1" (
    echo [1/2] 启动文件夹监控 (CLI 单模型路径)...
    echo      监控目录: data\raw
    echo      检查间隔: 30秒
    echo      *** 注意: watcher 自动转换产物为 unregistered_converted，不会直接进入正式 catalog ***
    echo      *** 正式文献入库建议使用 v2 流程: stage_raw_pdfs_to_paper_raw -^> match_paper_raw_metadata -^> convert_paper_raw_batch -^> curate_paper_raw -^> commit_paper_raw_to_papers --apply ***
    echo      *** 如果不需要自动转换 data/raw 根目录，请手动运行 python -m src.server ***
    echo      注意: mineru-api 不会在本脚本中同时启动，避免 GPU 双模型 OOM。
    echo.
    start "MinerU-Watcher" cmd /c "set CUDA_PATH=%CUDA_PATH%&& set PATH=%CUDA_PATH%\bin;%PATH%&& set MINERU_RUNNER=cli&& set MINERU_REQUIRE_GPU=true&& set MINERU_BACKEND=hybrid-engine&& set MINERU_EFFORT=medium&& set MINERU_METHOD=auto&& conda activate mineru && python watcher.py --interval 30"
) else (
    echo [info] START_WATCHER=0: watcher disabled by default.
    echo       If enabled, watcher will automatically convert PDFs in data/raw
    echo       and may occupy GPU. Do not run watcher during benchmark or bulk import.
    echo       To enable watcher: set START_WATCHER=1 ^&^& start.bat
    echo.
)

echo 启动文献库 Web 服务 (端口8080)...
echo      访问: http://localhost:8080
echo      API文档: http://localhost:8080/docs
echo      Runtime: http://localhost:8080/status/runtime
echo.
python -m src.server

pause
