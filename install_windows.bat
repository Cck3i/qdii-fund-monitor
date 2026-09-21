@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QDII 基金监控 - 一键配置

echo ============================================
echo   QDII 基金监控 - 一键配置（Windows）
echo ============================================
echo.

echo [1/4] 检查 Python ...
python --version 2>nul
if errorlevel 1 (
  echo.
  echo  未检测到 Python。请先安装：
  echo    1) 打开 https://www.python.org/downloads/
  echo    2) 下载 Windows 安装包并运行
  echo    3) 安装第一页务必勾选 "Add python.exe to PATH"
  echo    4) 安装完成后，重新双击本文件
  echo.
  pause
  exit /b 1
)

echo [2/4] 安装依赖 requests ...
python -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple requests

echo [3/4] 注册每日 08:30 自动扫描任务 ...
schtasks /Create /TN "QDII基金监控每日扫描" /TR "\"%~dp0run_daily.bat\"" /SC DAILY /ST 08:30 /F
if errorlevel 1 (
  echo   自动注册失败，请改用"任务计划程序"手动创建（见 每日自动运行说明.md 第 2 节）。
) else (
  echo   已注册：任务名 "QDII基金监控每日扫描"，每天 08:30 自动运行。
)

echo [4/4] 立即执行一次扫描（验证配置）...
call "%~dp0run_daily.bat"
echo.
echo 已完成。若 run_log.txt 显示"已发送至"，说明邮箱通知已生效。
pause
