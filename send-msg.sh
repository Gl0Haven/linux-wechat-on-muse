#!/bin/bash
# 向当前聚焦的微信聊天发送文本（经剪贴板粘贴，支持中文）
# 用法: send-msg.sh "要发送的文本"
export DISPLAY=:99
TEXT="$1"
if [ -z "$TEXT" ]; then echo "empty text"; exit 1; fi

# 获取微信窗口
WID=$(xdotool search --name "Weixin" 2>/dev/null | head -1)
if [ -z "$WID" ]; then echo "wechat window not found"; exit 1; fi
eval $(xdotool getwindowgeometry --shell $WID 2>/dev/null)

# 用 Python 设置剪贴板并持续处理事件（保持 12 秒）
python3 -c "
import tkinter as tk, sys, time
text = sys.argv[1]
r = tk.Tk()
r.withdraw()
r.clipboard_clear()
r.clipboard_append(text)
# 持续 update 12 秒，保持剪贴板所有权
end = time.time() + 12
while time.time() < end:
    r.update()
    time.sleep(0.1)
" "$TEXT" &
PYPID=$!
sleep 1.5

# 点击输入框
INPUT_X=$((WX + WIDTH/2 + 100))
INPUT_Y=$((WY + HEIGHT - 80))
xdotool mousemove $INPUT_X $INPUT_Y click 1
sleep 0.8
xdotool key ctrl+v
sleep 0.8
xdotool key Return
sleep 1

# 清理
kill $PYPID 2>/dev/null
wait $PYPID 2>/dev/null
echo "sent: $TEXT"
