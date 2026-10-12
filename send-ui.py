#!/usr/bin/env python3
"""
send-ui.py - 微信 GUI 原子操作（纯 python-xlib，无 xdotool/scrot 依赖）

用法:
  send-ui.py focus                      # 设输入焦点到微信主窗口
  send-ui.py winpos                      # 输出窗口左上角绝对坐标 "X Y"
  send-ui.py search "<群名>"             # Ctrl+F→清空→粘贴→Enter，打开聊天
  send-ui.py shot_title <输出png>        # 截标题固定区域（窗口相对坐标）
  send-ui.py click_input                 # 点击输入框（窗口相对坐标）
  send-ui.py paste_text "<文本>"          # 点输入框→Ctrl+A→粘贴
  send-ui.py press_enter                 # Enter
  send-ui.py clear_search                # Esc
  send-ui.py click_chat <x> <y>          # 点击聊天列表位置（窗口相对坐标），直接切换聊天

退出码: 0 成功，非 0 失败
"""
import sys
import time
import os
import subprocess

from Xlib import X, display
from Xlib.ext.xtest import fake_input
from Xlib import XK

os.environ["DISPLAY"] = ":99"

# 窗口相对坐标
REL_TITLE = (319, 33, 519, 73)
REL_INPUT = (556, 587)

def get_weixin():
    d = display.Display(':99')
    for w in d.screen().root.query_tree().children:
        try:
            if (w.get_wm_name() or '') == 'Weixin':
                return d, w
        except Exception:
            pass
    d.close()
    return None, None

def keycode(d, keysym_name):
    return d.keysym_to_keycode(XK.string_to_keysym(keysym_name))

def press_key(d, *keys):
    """按组合键，如 press_key(d, 'ctrl', 'f')。"""
    kcs = []
    for k in keys:
        if k == 'ctrl':
            kc = d.keysym_to_keycode(XK.string_to_keysym('Control_L'))
        elif k == 'shift':
            kc = d.keysym_to_keycode(XK.string_to_keysym('Shift_L'))
        elif k == 'alt':
            kc = d.keysym_to_keycode(XK.string_to_keysym('Alt_L'))
        else:
            kc = d.keysym_to_keycode(XK.string_to_keysym(k))
        kcs.append(kc)
    # 按下修饰键
    for kc in kcs[:-1]:
        fake_input(d, X.KeyPress, kc); d.sync(); time.sleep(0.05)
    # 按主体键
    fake_input(d, X.KeyPress, kcs[-1]); d.sync(); time.sleep(0.1)
    fake_input(d, X.KeyRelease, kcs[-1]); d.sync(); time.sleep(0.05)
    # 释放修饰键（逆序）
    for kc in reversed(kcs[:-1]):
        fake_input(d, X.KeyRelease, kc); d.sync(); time.sleep(0.05)

def cmd_focus():
    d, w = get_weixin()
    if not w:
        print("weixin not found", file=sys.stderr)
        return 1
    w.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()
    d.close()
    return 0

def cmd_winpos():
    d, w = get_weixin()
    if not w:
        print("weixin not found", file=sys.stderr)
        return 1
    g = w.get_geometry()
    print(f"{g.x} {g.y}")
    d.close()
    return 0

def cmd_search(name):
    if not name:
        print("missing name", file=sys.stderr)
        return 1
    d, w = get_weixin()
    if not w:
        print("weixin not found", file=sys.stderr)
        return 1
    w.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()
    time.sleep(0.3)  # 2026-10-08 提速：0.5→0.3
    # Ctrl+F
    press_key(d, 'ctrl', 'f')
    time.sleep(0.5)  # 2026-10-08 提速：0.8→0.5
    # Ctrl+A, BackSpace
    press_key(d, 'ctrl', 'a')
    time.sleep(0.2)  # 2026-10-08 提速：0.3→0.2
    press_key(d, 'BackSpace')
    time.sleep(0.3)  # 2026-10-08 提速：0.5→0.3
    # 粘贴（clipboard）
    clip = subprocess.Popen(
        [sys.executable, "~/workspace/wechat-bridge/clip.py", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.7)  # 2026-10-08 提速：1.2→0.7
    press_key(d, 'ctrl', 'v')
    time.sleep(2.5)  # 2026-10-08 提速：3.0→2.5（等搜索结果；1.5太短会导致点错重试更慢）
    # 点击左侧 Group Chats 下的群（约 400, 220）
    # 注意：按回车会进右侧聊天记录，必须点左边的群
    root = d.screen().root
    root.warp_pointer(400, 220)
    d.sync()
    time.sleep(0.2)  # 2026-10-08 提速：0.3→0.2
    fake_input(d, X.ButtonPress, 1); d.sync(); time.sleep(0.15)
    fake_input(d, X.ButtonRelease, 1); d.sync()
    time.sleep(0.8)  # 2026-10-08 提速：1.5→0.8（等聊天加载）
    clip.terminate()
    d.close()
    return 0

def cmd_shot_title(out):
    if not out:
        print("missing output", file=sys.stderr)
        return 1
    d, w = get_weixin()
    if not w:
        print("weixin not found", file=sys.stderr)
        return 1
    g = w.get_geometry()
    x1, y1, x2, y2 = REL_TITLE
    ax1, ay1 = g.x + x1, g.y + y1
    wdt, hgt = x2 - x1, y2 - y1
    # 用 xshot.py 截
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "xshot", os.path.join(os.path.dirname(os.path.abspath(__file__)), "xshot.py"))
    mod = importlib.util.module_from_spec(spec)
    # 直接调用 screenshot 函数
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from xshot import screenshot
    screenshot(out, ax1, ay1, wdt, hgt)
    d.close()
    return 0

def cmd_click_input():
    d, w = get_weixin()
    if not w:
        print("weixin not found", file=sys.stderr)
        return 1
    try:
        g = w.get_geometry()
        ix, iy = g.x + REL_INPUT[0], g.y + REL_INPUT[1]
        print(f"click at ({ix},{iy}) win=({g.x},{g.y})", file=sys.stderr)
    except Exception as e:
        print(f"geometry failed: {e}", file=sys.stderr)
        return 1
    w.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()
    time.sleep(0.2)
    root = d.screen().root
    root.warp_pointer(ix, iy)
    d.sync()
    time.sleep(0.2)
    fake_input(d, X.ButtonPress, 1); d.sync(); time.sleep(0.15)
    fake_input(d, X.ButtonRelease, 1); d.sync()
    time.sleep(0.5)
    d.close()
    return 0

def cmd_paste_text(text):
    if not text:
        print("missing text", file=sys.stderr)
        return 1
    rc = cmd_click_input()
    if rc != 0:
        return rc
    d = display.Display(':99')
    # Ctrl+A
    press_key(d, 'ctrl', 'a')
    time.sleep(0.2)  # 2026-10-08 提速：0.3→0.2
    # 2026-10-11 修复：改用 xclip 直接设置文本（替代易静默失败的 clip.py tkinter 方案）
    # 用 stdin 传递，避免命令行编码问题
    try:
        import subprocess as _sp
        import os as _os
        _xclip = _os.path.expanduser("~/bin/xclip")
        proc = _sp.Popen(
            [_xclip, "-selection", "clipboard", "-i"],
            stdin=_sp.PIPE, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            env={**__import__('os').environ, "DISPLAY": ":99"})
        _, err = proc.communicate(input=text.encode('utf-8'), timeout=5)
        if proc.returncode != 0:
            print(f"xclip failed rc={proc.returncode}: {err[:200]}", file=sys.stderr)
            d.close()
            return 1
    except Exception as e:
        print(f"xclip exception: {e}", file=sys.stderr)
        d.close()
        return 1
    time.sleep(0.4)  # 等剪贴板落定
    press_key(d, 'ctrl', 'v')
    time.sleep(0.4)  # 2026-10-08 提速：0.8→0.4（等粘贴落定）
    d.close()
    return 0

def cmd_press_enter():
    d = display.Display(':99')
    press_key(d, 'Return')
    time.sleep(0.3)  # 2026-10-08 提速：0.5→0.3
    d.close()
    return 0

def cmd_press_esc():
    """按 Esc（2026-10-09 图片查看器关闭用）。"""
    d = display.Display(':99')
    press_key(d, 'Escape')
    time.sleep(0.5)
    d.close()
    return 0

def cmd_type_text(text):
    """XTEST 逐字打出可打印 ASCII（用于短占位符，比剪贴板粘贴快得多）。
    占位符只用 [a-z0-9]，无 shift 问题；为通用性保留大写支持。"""
    import re
    if not text:
        print("missing text", file=sys.stderr)
        return 1
    if not re.fullmatch(r'[\x20-\x7E]+', text):
        print("type_text: printable ASCII only", file=sys.stderr)
        return 1
    d = display.Display(':99')
    shift_kc = d.keysym_to_keycode(XK.string_to_keysym('Shift_L'))
    # 先全选（替换语义，与 paste_text 一致，保证输入框内容 == 占位符）
    press_key(d, 'ctrl', 'a')
    time.sleep(0.15)
    for ch in text:
        shift = False
        if 'A' <= ch <= 'Z':
            ks_name = ch.lower()
            shift = True
        else:
            ks_name = ch
        kc = d.keysym_to_keycode(XK.string_to_keysym(ks_name))
        if kc == 0:
            print(f"type_text: no keycode for {ch!r}", file=sys.stderr)
            d.close()
            return 1
        if shift:
            fake_input(d, X.KeyPress, shift_kc)
        fake_input(d, X.KeyPress, kc)
        d.sync()
        fake_input(d, X.KeyRelease, kc)
        if shift:
            fake_input(d, X.KeyRelease, shift_kc)
        d.sync()
    d.close()
    return 0

def cmd_at_mentions(names):
    """批量真 @：按倒序逐个调用 at_mention，保证最终顺序正确。
    2026-10-09 23:14 优化：多 @ 时每个都 Home 会导致倒序堆叠，
    倒序执行则最终顺序正确。
    用法: send-ui.py at_mentions "[friend2],[friend3],[user]"
    """
    if not names:
        print("missing names", file=sys.stderr)
        return 1
    name_list = [n.strip() for n in names.split(",") if n.strip()]
    if not name_list:
        print("no valid names", file=sys.stderr)
        return 1
    # 倒序执行，保证最终 @ 顺序与输入一致
    for name in reversed(name_list):
        rc = cmd_at_mention(name)
        if rc != 0:
            print(f"at_mentions: failed at @{name}", file=sys.stderr)
            return rc
        time.sleep(0.5)
    print(f"at_mentions: {len(name_list)} done", flush=True)
    return 0

def cmd_at_mention(name):
    """真 @ 成员（2026-10-11 重构：按弹窗序号选，不按名字过滤）。
    2026-10-09 22:50 用户要求：bot 回复要用真正的微信 @（带通知），
    而不是纯文本写名字。
    2026-10-11 用户纠正：微信群 @ 用的是群昵称，不是备注。之前按名字过滤
    的逻辑（@大号 实际要 @[user]）根本跑不通。改成按 at_popup_order.json
    的固定序号：@ → Down×(n-1) → 回车，n=1 不按 Down。
    [主群]：[friend1](1) / [user](2) / [friend2](3) / [friend3](4)
    名字到序号的映射（含备注、群昵称、wxid 各种叫法）：
    """
    # 名字 → 弹窗序号（1-based）。支持备注、群昵称混用。
    # 注意：以下为示例占位，实际使用时按你自己群的 at_popup_order.json 填写。
    NAME_TO_POS = {
        # 用户本人
        "大号": 2, "[user]": 2,
        "wxid_YOUR_MAIN_ACCOUNT": 2,
        # [friend1]
        "[friend1]": 1, "wxid_REDACTED_1": 1,
        # [friend2]（群昵称已改，但序号不变）
        "[friend2]": 3, "@[friend1]@[user]@[friend2]": 3,
        "wxid_REDACTED_2": 3,
        # [friend3]
        "[friend3]": 4, "wxid_REDACTED_3": 4,
    }
    pos = NAME_TO_POS.get(name)
    if not pos:
        print(f"at_mention: unknown name {name!r}, 降级为纯文本", file=sys.stderr)
        return 1
    if not name:
        print("missing name", file=sys.stderr)
        return 1
    from Xlib import X, XK
    from Xlib import display
    import subprocess
    d = display.Display(':99')
    shift_kc = d.keysym_to_keycode(XK.string_to_keysym('Shift_L'))
    ctrl_kc = d.keysym_to_keycode(XK.string_to_keysym('Control_L'))

    # 1. 按 Home 到开头（@ 插在最前面）
    home_kc = d.keysym_to_keycode(XK.string_to_keysym('Home'))
    fake_input(d, X.KeyPress, home_kc)
    d.sync()
    fake_input(d, X.KeyRelease, home_kc)
    d.sync()
    time.sleep(0.3)

    # 2. 敲 "@"：Shift+2 真实按键
    at_kc = d.keysym_to_keycode(XK.string_to_keysym('2'))
    fake_input(d, X.KeyPress, shift_kc)
    fake_input(d, X.KeyPress, at_kc)
    d.sync()
    fake_input(d, X.KeyRelease, at_kc)
    fake_input(d, X.KeyRelease, shift_kc)
    d.sync()
    # 3. 等成员弹窗出现
    time.sleep(1.8)

    # 4-5. 按序号选：Down×(pos-1) → 回车（2026-10-11 重构）
    # 不再按名字过滤（群昵称/备注/本名三套体系太乱，直接按固定序号最可靠）
    time.sleep(0.5)
    if pos > 1:
        down_kc = d.keysym_to_keycode(XK.string_to_keysym('Down'))
        for _ in range(pos - 1):
            fake_input(d, X.KeyPress, down_kc)
            d.sync()
            fake_input(d, X.KeyRelease, down_kc)
            d.sync()
            time.sleep(0.3)
    enter_kc = d.keysym_to_keycode(XK.string_to_keysym('Return'))
    fake_input(d, X.KeyPress, enter_kc)
    d.sync()
    fake_input(d, X.KeyRelease, enter_kc)
    d.sync()
    time.sleep(0.5)
    # 6. 补个空格，@ 和正文分开
    space_kc = d.keysym_to_keycode(XK.string_to_keysym('space'))
    fake_input(d, X.KeyPress, space_kc)
    d.sync()
    fake_input(d, X.KeyRelease, space_kc)
    d.sync()
    d.close()
    print(f"at_mention: @{name} done", flush=True)
    return 0

def cmd_click_chat(x, y):
    """点击聊天列表位置（窗口相对坐标）。用于直接切换聊天，不经过搜索。"""
    try:
        x, y = int(x), int(y)
    except (ValueError, TypeError):
        print("invalid coordinates", file=sys.stderr)
        return 1
    d, w = get_weixin()
    if not w:
        print("weixin not found", file=sys.stderr)
        return 1
    g = w.get_geometry()
    ax, ay = g.x + x, g.y + y
    w.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()
    time.sleep(0.3)
    root = d.screen().root
    root.warp_pointer(ax, ay)
    d.sync()
    time.sleep(0.3)
    fake_input(d, X.ButtonPress, 1); d.sync(); time.sleep(0.15)
    fake_input(d, X.ButtonRelease, 1); d.sync()
    time.sleep(1.0)  # 等待聊天切换
    d.close()
    return 0

def cmd_scroll_top():
    """把聊天列表滑到最顶部（置顶的聊天才能被点到）。"""
    d, w = get_weixin()
    if not w:
        print("weixin not found", file=sys.stderr)
        return 1
    g = w.get_geometry()
    # 点一下聊天列表区域聚焦（约 Y=200）
    ax, ay = g.x + 150, g.y + 200
    root = d.screen().root
    root.warp_pointer(ax, ay)
    d.sync()
    time.sleep(0.3)
    # 滚轮向上多次（button 4 = wheel up）
    for _ in range(8):
        fake_input(d, X.ButtonPress, 4); d.sync(); time.sleep(0.05)
        fake_input(d, X.ButtonRelease, 4); d.sync(); time.sleep(0.05)
    time.sleep(0.8)
    d.close()
    return 0

def cmd_clear_search():
    d = display.Display(':99')
    press_key(d, 'Escape')
    time.sleep(0.5)
    d.close()
    return 0

def cmd_right_click(x, y):
    """在指定位置右键点击（窗口相对坐标）。用于调出消息右键菜单。"""
    try:
        x, y = int(x), int(y)
    except (ValueError, TypeError):
        print("invalid coordinates", file=sys.stderr)
        return 1
    d, w = get_weixin()
    if not w:
        print("weixin not found", file=sys.stderr)
        return 1
    g = w.get_geometry()
    ax, ay = g.x + x, g.y + y
    root = d.screen().root
    root.warp_pointer(ax, ay)
    d.sync()
    time.sleep(0.3)
    # 右键 = button 3
    fake_input(d, X.ButtonPress, 3); d.sync(); time.sleep(0.15)
    fake_input(d, X.ButtonRelease, 3); d.sync()
    time.sleep(0.8)  # 等待右键菜单弹出
    d.close()
    return 0

def cmd_click_quote_menu():
    """点击右键菜单中的"引用"选项。
    微信右键菜单中"引用"通常在靠上位置，这里按相对菜单的偏移点击。
    注意：调用前必须已经右键调出菜单。
    """
    # 右键菜单弹出后，"引用"选项的位置需要实测
    # 这里先按一个经验偏移，实际使用时需要校准
    d, w = get_weixin()
    if not w:
        print("weixin not found", file=sys.stderr)
        return 1
    # 获取鼠标当前位置（右键菜单通常在鼠标附近弹出）
    # 我们按菜单项的相对位置点击
    # TODO: 需要实测"引用"在菜单中的位置
    d.close()
    return 0

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("usage: send-ui.py {focus|winpos|search|shot_title|click_input|paste_text|press_enter|clear_search|click_chat}", file=sys.stderr)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == 'focus':
        sys.exit(cmd_focus())
    elif cmd == 'winpos':
        sys.exit(cmd_winpos())
    elif cmd == 'search':
        sys.exit(cmd_search(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif cmd == 'shot_title':
        sys.exit(cmd_shot_title(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif cmd == 'click_input':
        sys.exit(cmd_click_input())
    elif cmd == 'paste_text':
        sys.exit(cmd_paste_text(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif cmd == 'press_enter':
        sys.exit(cmd_press_enter())
    elif cmd == 'press_esc':
        sys.exit(cmd_press_esc())
    elif cmd == 'type_text':
        sys.exit(cmd_type_text(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif cmd == 'at_mention':
        sys.exit(cmd_at_mention(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif cmd == 'at_mentions':
        sys.exit(cmd_at_mentions(sys.argv[2] if len(sys.argv) > 2 else ""))
    elif cmd == 'clear_search':
        sys.exit(cmd_clear_search())
    elif cmd == 'click_chat':
        if len(sys.argv) < 4:
            print("usage: send-ui.py click_chat <x> <y>", file=sys.stderr)
            sys.exit(1)
        sys.exit(cmd_click_chat(sys.argv[2], sys.argv[3]))
    elif cmd == 'scroll_top':
        sys.exit(cmd_scroll_top())
    elif cmd == 'right_click':
        if len(sys.argv) < 4:
            print("usage: send-ui.py right_click <x> <y>", file=sys.stderr)
            sys.exit(1)
        sys.exit(cmd_right_click(sys.argv[2], sys.argv[3]))
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
