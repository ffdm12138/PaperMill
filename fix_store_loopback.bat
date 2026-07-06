@echo off
chcp 65001 >nul
title Microsoft Store 环路回豁免修复工具
echo =============================================
echo    正在为 Microsoft Store 开启环路回豁免
echo =============================================
echo.

echo [1/3] 添加 Microsoft.WindowsStore...
CheckNetIsolation LoopbackExempt -a -n=Microsoft.WindowsStore_8wekyb3d8bbwe
if %errorlevel% equ 0 (echo   ✓ 成功) else (echo   ✗ 失败，请以管理员身份运行此脚本)

echo [2/3] 添加 StorePurchaseApp...
CheckNetIsolation LoopbackExempt -a -n=Microsoft.StorePurchaseApp_8wekyb3d8bbwe
if %errorlevel% equ 0 (echo   ✓ 成功) else (echo   ✗ 失败)

echo [3/3] 添加 Store Engagement...
CheckNetIsolation LoopbackExempt -a -n=Microsoft.Services.Store.Engagement_8wekyb3d8bbwe
if %errorlevel% equ 0 (echo   ✓ 成功) else (echo   ✗ 失败)

echo.
echo ====== 当前环路回豁免列表 ======
CheckNetIsolation LoopbackExempt -s
echo ================================
echo.
echo 操作完成！请重启 Microsoft Store 后测试。
echo 按任意键退出...
pause >nul
