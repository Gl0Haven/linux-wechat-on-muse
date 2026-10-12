#!/usr/bin/env python3
"""手动表情发送脚本（text/html 动画表情）
用法: python3 send_sticker_manual.py <chat_id> <md5> <gif_path>

2026-10-12 根因修复（用户分析）：
1. X11 Selection 竞态：xclip 是所有者挂后台等请求，不能回车后立刻 terminate
2. 时序：先点击输入框，再写剪贴板（避免点击扰动 Selection）
3. 延长 xclip 存活期，给微信充分时间解析 HTML + 读本地 gif
4. 子进程内也要导航到目标群（不能假设父进程已导航成功）
"""
import sys
import os
import time
import subprocess
import tempfile

def main():
    if len(sys.argv) != 4:
        print(f"用法: {sys.argv[0]} <chat_id> <md5> <gif_path>", file=sys.stderr)
        return 1
    chat_id = sys.argv[1]
    md5 = sys.argv[2]
    gif_path = sys.argv[3]
    
    # 2026-10-12 修复：DB 里存的是相对路径，必须转绝对路径
    # 否则 HTML 的 file:// URL 无效，微信读不到文件，粘贴后无预览
    if not os.path.isabs(gif_path):
        gif_path = os.path.join("~/workspace/wechat-bridge", gif_path)
    gif_path = os.path.abspath(gif_path)
    
    # 防御性断言：文件必须存在且非空
    if not os.path.isfile(gif_path) or os.path.getsize(gif_path) == 0:
        print(f"GIF 文件异常: {gif_path}", file=sys.stderr)
        return 1
    
    html = f'<img src="file://{gif_path}" data-type="emoji" data-md5="{md5}" />'
    env = {**os.environ, "DISPLAY": ":99"}
    bridge_dir = "~/workspace/wechat-bridge"
    
    # 1. HTML 写临时文件
    with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, dir='/tmp') as f:
        f.write(html)
        html_path = f.name
    
    clip = None
    try:
        # 2. 先导航到目标群（子进程内独立导航，不依赖父进程）
        sys.path.insert(0, bridge_dir)
        from sendd_v2 import navigate
        ok, reason = navigate(chat_id)
        if not ok:
            print(f"导航失败: {reason}", file=sys.stderr)
            return 1
        time.sleep(0.5)
        
        # 3. 再聚焦输入框（直接调用，不用子进程，避免嵌套 hang）
        # 2026-10-12: subprocess 嵌套导致 X11 hang，改直接 import
        sys.path.insert(0, bridge_dir)
        import importlib.util
        spec = importlib.util.spec_from_file_location("send_ui", os.path.join(bridge_dir, "send-ui.py"))
        send_ui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(send_ui)
        rc = send_ui.cmd_click_input()
        if rc != 0:
            print(f"click_input 失败: rc={rc}", file=sys.stderr)
            return 1
        time.sleep(0.5)
        
        # 3. 此时再启动 xclip 写入剪贴板
        clip = subprocess.Popen(
            ["~/bin/xclip", "-selection", "clipboard", "-t", "text/html",
             "-i", html_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        time.sleep(1.0)  # 等 xclip 稳定声明所有权
        # 检查 xclip 是否还活着
        if clip.poll() is not None:
            _, err = clip.communicate()
            print(f"xclip 已退出! rc={clip.returncode}, err={err[:200]}", file=sys.stderr)
            return 1
        print(f"xclip 运行中, pid={clip.pid}", file=sys.stderr)
        
        # 4. Ctrl+V（用 Xlib，xdotool 在子进程下可能 hang）
        # 2026-10-12: xdotool 替换 Xlib 后出现随机 hang，换回
        from Xlib import X, XK, display as _d
        from Xlib.ext.xtest import fake_input as _fi
        d = _d.Display(':99')
        ctrl = d.keysym_to_keycode(XK.string_to_keysym('Control_L'))
        v = d.keysym_to_keycode(XK.string_to_keysym('v'))
        _fi(d, X.KeyPress, ctrl); d.sync(); time.sleep(0.05)
        _fi(d, X.KeyPress, v); d.sync(); time.sleep(0.1)
        _fi(d, X.KeyRelease, v); d.sync(); time.sleep(0.05)
        _fi(d, X.KeyRelease, ctrl); d.sync()
        d.close()
        
        time.sleep(5.0)  # 给微信充分时间解析 HTML 并读入本地 gif（daemon 下可能慢）
        
        # 5. 回车发送（直接调用，避免子进程嵌套）
        rc2 = send_ui.cmd_press_enter()
        if rc2 != 0:
            print(f"press_enter 失败: rc={rc2}", file=sys.stderr)
            return 1
        
        # 6. 延迟清理：让 xclip 多活 5 秒，确保微信读完
        time.sleep(5.0)
        
        print("ok", flush=True)
        return 0
    except Exception as e:
        print(f"异常: {e}", file=sys.stderr)
        return 1
    finally:
        # 延迟后再清理（不在回车后立刻杀）
        if clip:
            try:
                clip.terminate()
            except:
                pass
        try:
            os.unlink(html_path)
        except:
            pass

if __name__ == "__main__":
    sys.exit(main())
