#!/bin/bash
# 启动虚拟显示器 + 窗口管理器
export DISPLAY=:99
pkill -f "Xvfb :99" 2>/dev/null
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x800x24 &
sleep 2
openbox --display :99 &
sleep 1
echo "Xvfb ready on :99"
