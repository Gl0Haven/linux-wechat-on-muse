#!/usr/bin/env python3
"""X11 Selection 监控：验证 WeChat 是否在 Ctrl+V 后请求 text/html 数据。
用法: python3 selection_monitor.py
"""
import os, time, sys
os.environ["DISPLAY"] = ":99"

from Xlib import X, display
from Xlib.protocol import event

d = display.Display(':99')
screen = d.screen()
root = screen.root

# 创建一个窗口来拥有 CLIPBOARD selection
win = root.create_window(0, 0, 1, 1, 0, screen.root_depth)
html = b'<img src="file:///tmp/test.gif" />'

# 声明为 CLIPBOARD 所有者
CLIPBOARD = d.intern_atom('CLIPBOARD')
TEXT_HTML = d.intern_atom('text/html')
TARGETS = d.intern_atom('TARGETS')

win.set_selection_owner(CLIPBOARD, X.CurrentTime)
d.sync()
print(f"[{time.time():.2f}] 已声明 CLIPBOARD 所有者", flush=True)

# 先点击输入框聚焦
import subprocess
env = {**os.environ, "DISPLAY": ":99"}
r = subprocess.run(['python3', 'send-ui.py', 'click_input'],
                   cwd='~/workspace/wechat-bridge',
                   capture_output=True, timeout=15, env=env)
print(f"[{time.time():.2f}] click_input rc={r.returncode}", flush=True)
time.sleep(1.0)

# 模拟 Ctrl+V（让微信来请求数据）
from Xlib.ext.xtest import fake_input
from Xlib import XK
time.sleep(0.5)
ctrl = d.keysym_to_keycode(XK.string_to_keysym('Control_L'))
v = d.keysym_to_keycode(XK.string_to_keysym('v'))
print(f"[{time.time():.2f}] 发送 Ctrl+V", flush=True)
fake_input(d, X.KeyPress, ctrl); d.sync()
fake_input(d, X.KeyPress, v); d.sync()
fake_input(d, X.KeyRelease, v); d.sync()
fake_input(d, X.KeyRelease, ctrl); d.sync()

# 监听 SelectionRequest 事件，最多等 5 秒
print(f"[{time.time():.2f}] 等待 SelectionRequest...", flush=True)
d.sync()
start = time.time()
got_request = False
while time.time() - start < 5.0:
    if d.pending_events() > 0:
        ev = d.next_event()
        print(f"[{time.time():.2f}] 收到事件: {ev.__class__.__name__}", flush=True)
        if isinstance(ev, event.SelectionRequest):
            print(f"[{time.time():.2f}] *** SelectionRequest! target={d.get_atom_name(ev.target)} ***", flush=True)
            got_request = True
            # 回复空数据（模拟）
            break
    time.sleep(0.05)

if not got_request:
    print(f"[{time.time():.2f}] 超时：未收到 SelectionRequest", flush=True)
else:
    print(f"[{time.time():.2f}] 成功收到请求", flush=True)

d.close()
