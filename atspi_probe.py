#!/usr/bin/env python3
"""
AT-SPI 探测脚本：检查微信是否暴露无障碍控件树
使用前需：QT_ACCESSIBILITY=1 启动微信 + D-Bus 会话总线运行
"""
import os
import sys

def main():
    # 检查环境
    if os.environ.get('QT_ACCESSIBILITY') != '1':
        print("WARN: QT_ACCESSIBILITY 未设置为 1，微信可能不暴露控件树")
    
    try:
        import gi
        gi.require_version('Atspi', '2.0')
        from gi.repository import Atspi
    except ImportError as e:
        print(f"FAIL: 缺少 pygobject/Atspi 绑定: {e}")
        print("安装: sudo apt install python3-gi gir1.2-atspi-2.0")
        return 1
    
    # 初始化
    Atspi.init()
    
    # 获取桌面根
    desktop = Atspi.get_desktop(0)
    print(f"桌面子节点数: {desktop.get_child_count()}")
    
    # 找微信窗口
    found = False
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        try:
            name = app.get_name()
            if name and 'weixin' in name.lower() or 'wechat' in name.lower():
                print(f"找到微信应用: {name}")
                found = True
                # 递归打印控件树（前3层）
                print_tree(app, 0, max_depth=3)
                break
        except:
            pass
    
    if not found:
        print("未找到微信应用节点（可能未启用无障碍或未运行）")
        return 1
    return 0

def print_tree(node, depth, max_depth=3):
    if depth > max_depth:
        return
    try:
        name = node.get_name() or "(无名)"
        role = node.get_role_name()
        print(f"{'  '*depth}- {role}: {name}")
        for i in range(min(node.get_child_count(), 20)):  # 限制每层20个
            child = node.get_child_at_index(i)
            print_tree(child, depth+1, max_depth)
    except Exception as e:
        pass

if __name__ == '__main__':
    sys.exit(main())
