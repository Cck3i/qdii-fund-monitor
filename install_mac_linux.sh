#!/bin/bash
# QDII 基金监控 - 一键配置（macOS / Linux）
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"

echo "============================================"
echo "  QDII 基金监控 - 一键配置（macOS / Linux）"
echo "============================================"

PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "未检测到 Python3。macOS 可执行：brew install python3 ；或到 https://www.python.org/downloads/ 下载安装。"
  exit 1
fi
echo "[1/3] Python: $PY"

echo "[2/3] 安装依赖 requests ..."
"$PY" -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple requests || true

chmod +x run_daily.sh

if [ "$(uname)" = "Darwin" ]; then
  echo "[3/3] 写入 launchd 定时任务（每天 08:30）..."
  PLIST="$HOME/Library/LaunchAgents/com.marvis.qdiimonitor.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.marvis.qdiimonitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/run_daily.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>$DIR/run_log.txt</string>
  <key>StandardErrorPath</key><string>$DIR/run_log.txt</string>
</dict>
</plist>
EOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "   已注册 launchd 任务：com.marvis.qdiimonitor（每天 08:30）"
else
  echo "[3/3] 写入 crontab 定时任务（每天 08:30）..."
  LINE="30 8 * * * cd $DIR && $PY run_daily.sh >/dev/null 2>&1"
  ( crontab -l 2>/dev/null | grep -v 'run_daily.sh' ; echo "$LINE" ) | crontab -
  echo "   已写入 crontab：$LINE"
fi

echo "立即执行一次扫描（验证配置）..."
./run_daily.sh
echo "完成。若 run_log.txt 显示已发送，说明邮箱通知已生效。"
