#!/usr/bin/env python3
"""设置 X clipboard（tkinter），保持运行直到被 kill。"""
import tkinter as tk
import sys
import time

text = sys.argv[1] if len(sys.argv) > 1 else ""
r = tk.Tk()
r.withdraw()
r.clipboard_clear()
r.clipboard_append(text)
# 保持运行，持有 clipboard 所有权
try:
    while True:
        r.update()
        time.sleep(0.1)
except KeyboardInterrupt:
    pass
