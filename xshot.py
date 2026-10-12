#!/usr/bin/env python3
"""用 python-xlib 截屏（替代 scrot），保存到指定路径。"""
import sys
from Xlib import display
from PIL import Image

def screenshot(out_path, x=0, y=0, w=1280, h=800):
    d = display.Display(':99')
    root = d.screen().root
    raw = root.get_image(x, y, w, h, 2, 0xffffffff)  # ZPixmap
    # XImage 数据是 BGRA，需要转换
    import struct
    data = raw.data
    # 创建 PIL 图像（假设 32-bit）
    img = Image.frombytes('RGBX', (w, h), data, 'raw', 'BGRX')
    img = img.convert('RGB')
    img.save(out_path)
    d.close()

if __name__ == '__main__':
    out = sys.argv[1] if len(sys.argv) > 1 else '/tmp/live_screen.png'
    if len(sys.argv) == 6:
        screenshot(out, int(sys.argv[2]), int(sys.argv[3]),
                   int(sys.argv[4]), int(sys.argv[5]))
    else:
        screenshot(out)
    print(f"saved {out}")
