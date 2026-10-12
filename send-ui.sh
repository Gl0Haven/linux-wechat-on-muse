#!/bin/bash
# send-ui.sh - 微信 GUI 原子操作（bash + xdotool，Python 只做 X11 查询）
#
# 用法:
#   send-ui.sh focus                      # 设输入焦点到微信主窗口
#   send-ui.sh winpos                     # 输出窗口左上角绝对坐标 "X Y"
#   send-ui.sh search "<群名>"            # 搜索框聚焦→清空→粘贴→Enter，打开聊天
#   send-ui.sh shot_title <输出png>       # 截标题固定区域（窗口相对坐标）
#   send-ui.sh click_input                # 点击输入框（窗口相对坐标）
#   send-ui.sh paste                      # Ctrl+V
#   send-ui.sh press_enter                # Enter
#   send-ui.sh clear_search               # Esc 清掉搜索
#
# 约定: 所有坐标都是相对于微信窗口左上角，窗口位置动态获取。
# 标题区域（窗口相对）: (319,33)-(519,73)  [= 绝对 (500,100)-(700,140) @窗口(181,67)]
# 搜索框（窗口相对）: (219,53)
# 输入框（窗口相对）: (556,587)
#
# 退出码: 0 成功，非 0 失败（sendd 遇到非 0 直接 fail closed）

set -e
export DISPLAY=:99
export PATH="$HOME/bin:/usr/bin:/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# 窗口相对坐标（实测）
REL_TITLE_X1=319; REL_TITLE_Y1=33; REL_TITLE_X2=519; REL_TITLE_Y2=73
REL_SEARCH_X=219; REL_SEARCH_Y=15
REL_INPUT_X=556; REL_INPUT_Y=587

winpos() {
    DISPLAY=:99 python3 -c "
from Xlib import display
d = display.Display(':99')
for w in d.screen().root.query_tree().children:
    try:
        if (w.get_wm_name() or '') == 'Weixin':
            g = w.get_geometry()
            print(f'{g.x} {g.y}')
            break
    except Exception:
        pass
d.close()
"
}

cmd_focus() {
    DISPLAY=:99 python3 -c "
from Xlib import X, display
d = display.Display(':99')
for w in d.screen().root.query_tree().children:
    try:
        if (w.get_wm_name() or '') == 'Weixin':
            w.set_input_focus(X.RevertToParent, X.CurrentTime)
            d.sync()
            break
    except Exception:
        pass
d.close()
"
}

cmd_search() {
    local name="$1"
    [ -n "$name" ] || { echo "search: missing name" >&2; exit 1; }
    # 先聚焦（和搜索在同一个进程里，避免焦点丢失）
    cmd_focus
    sleep 0.5
    # Ctrl+F 聚焦搜索框（实测有效；点击坐标不可靠）
    xdotool key --clearmodifiers ctrl+f
    sleep 0.8
    xdotool key --clearmodifiers ctrl+a
    sleep 0.3
    xdotool key --clearmodifiers BackSpace
    sleep 0.5
    python3 ~/workspace/wechat-bridge/clip.py "$name" &
    local clippid=$!
    sleep 1.2
    xdotool key --clearmodifiers ctrl+v
    sleep 3.0
    xdotool key --clearmodifiers Return
    sleep 1.5
    kill "$clippid" 2>/dev/null || true
    wait "$clippid" 2>/dev/null || true
}

cmd_shot_title() {
    local out="$1"
    [ -n "$out" ] || { echo "shot_title: missing output" >&2; exit 1; }
    read -r WX WY < <(winpos)
    [ -n "$WX" ] || { echo "shot_title: window not found" >&2; exit 1; }
    local x1=$((WX + REL_TITLE_X1)); local y1=$((WY + REL_TITLE_Y1))
    local x2=$((WX + REL_TITLE_X2)); local y2=$((WY + REL_TITLE_Y2))
    scrot -a "$x1,$y1,$((x2-x1)),$((y2-y1))" "$out"
}

cmd_click_input() {
    read -r WX WY < <(winpos)
    [ -n "$WX" ] || { echo "click_input: window not found" >&2; exit 1; }
    local ix=$((WX + REL_INPUT_X))
    local iy=$((WY + REL_INPUT_Y))
    xdotool mousemove "$ix" "$iy" click 1
    sleep 0.8
}

cmd_paste_text() {
    # $1 = 要粘贴的文本；内部处理 clipboard（中文安全）
    local text="$1"
    [ -n "$text" ] || { echo "paste_text: missing text" >&2; exit 1; }
    cmd_click_input
    xdotool key --clearmodifiers ctrl+a
    sleep 0.3
    python3 ~/workspace/wechat-bridge/clip.py "$text" &
    local clippid=$!
    sleep 1.2
    xdotool key --clearmodifiers ctrl+v
    sleep 0.8
    kill "$clippid" 2>/dev/null || true
    wait "$clippid" 2>/dev/null || true
}

cmd_paste() {
    xdotool key --clearmodifiers ctrl+v
    sleep 0.8
}

cmd_press_enter() {
    xdotool key --clearmodifiers Return
    sleep 0.5
}

cmd_clear_search() {
    xdotool key --clearmodifiers Escape
    sleep 0.5
}

case "$1" in
    focus)        cmd_focus ;;
    winpos)       winpos ;;
    search)       cmd_search "$2" ;;
    shot_title)   cmd_shot_title "$2" ;;
    click_input)  cmd_click_input ;;
    paste_text)   cmd_paste_text "$2" ;;
    paste)        cmd_paste ;;
    press_enter)  cmd_press_enter ;;
    clear_search) cmd_clear_search ;;
    *) echo "用法: $0 {focus|winpos|search <name>|shot_title <png>|click_input|paste_text <text>|paste|press_enter|clear_search}" >&2; exit 1 ;;
esac
