@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ===== %date% %time% ===== >> run_log.txt
python scan.py >> run_log.txt 2>&1
python notify.py >> run_log.txt 2>&1
echo 扫描完成，日志见 run_log.txt
