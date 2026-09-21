#!/bin/bash
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || command -v python)
echo "===== $(date '+%Y-%m-%d %H:%M:%S') =====" >> run_log.txt
"$PY" scan.py >> run_log.txt 2>&1
"$PY" notify.py >> run_log.txt 2>&1
