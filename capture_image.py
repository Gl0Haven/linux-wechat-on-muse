#!/usr/bin/env python3
"""图片截图兜底（2026-10-09 图片理解）。

只在需要理解图片时调用（@bot 或大号发的图），不做全量截图。
流程：检查 sendd 空闲 → 导航到聊天 → 截聊天窗口 → 可选：点击缩略图
      打开查看器 → 截全图 → Esc 关闭 → 回写 DB media_path。

用法：
  python3 capture_image.py <chat_uname> <msg_id>
      导航+截聊天窗口 → media/<msg_id>_chat.png（供 agent 定位缩略图）
  python3 capture_image.py <chat_uname> <msg_id> --click X Y
      在上面基础上点击 (X,Y) 打开查看器 → media/<msg_id>.png，
      Esc 关闭，回写 DB（media_path=..., needs_media=0）

输出：截图文件绝对路径（stdout 最后一行）。
"""
import os
import sqlite3
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
BRIDGE = os.path.join(HOME, "workspace/wechat-bridge")
MEDIA_DIR = os.path.join(BRIDGE, "media")
DB_PATH = os.path.join(BRIDGE, "queue.db")
SEND_UI = os.path.join(BRIDGE, "send-ui.py")

os.environ["DISPLAY"] = ":99"
sys.path.insert(0, BRIDGE)


def sendd_busy():
    try:
        db = sqlite3.connect(DB_PATH, timeout=5)
        n = db.execute(
            "SELECT COUNT(*) FROM messages WHERE status='SENDING'"
        ).fetchone()[0]
        db.close()
        return n > 0
    except Exception:
        return True  # 查不到就当忙，fail-closed


def shot(path):
    r = subprocess.run(["scrot", path], capture_output=True, timeout=20)
    return r.returncode == 0 and os.path.exists(path)


def ui_click(x, y):
    # send-ui.py click_chat 实为窗口相对坐标通用点击
    r = subprocess.run(
        ["python3", SEND_UI, "click_chat", str(x), str(y)],
        capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def ui_esc():
    r = subprocess.run(["python3", SEND_UI, "press_esc"],
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    chat_uname, msg_id = sys.argv[1], sys.argv[2]
    click_xy = None
    if "--click" in sys.argv:
        i = sys.argv.index("--click")
        click_xy = (int(sys.argv[i + 1]), int(sys.argv[i + 2]))

    os.makedirs(MEDIA_DIR, exist_ok=True)

    # 2026-10-11 R2: GUI 独占锁，与 sendd 互斥，防 TOCTOU
    from gui_lock import gui_locked
    try:
        with gui_locked(timeout=30):
            return _main_locked(chat_uname, msg_id, click_xy)
    except TimeoutError:
        print("skip: gui busy", flush=True)
        sys.exit(2)


def _main_locked(chat_uname, msg_id, click_xy):

    if sendd_busy():
        print("skip: sendd busy", flush=True)
        sys.exit(2)

    from sendd_v2 import navigate
    ok, reason = navigate(chat_uname)
    if not ok:
        print(f"navigate failed: {reason}", flush=True)
        sys.exit(3)
    time.sleep(1.0)

    chat_shot = os.path.join(MEDIA_DIR, f"{msg_id}_chat.png")
    if not shot(chat_shot):
        print("chat screenshot failed", flush=True)
        sys.exit(4)
    print(chat_shot, flush=True)
    # 2026-10-09 修：聊天截图后立即回写 DB（之前只在 --click 分支回写，
    # 导致 wal_reader 触发的不带 --click 调用从不设置 media_path）
    try:
        db = sqlite3.connect(DB_PATH, timeout=10)
        db.execute(
            "UPDATE messages SET media_path=?, updated_at=? WHERE id=?",
            (chat_shot, int(time.time()), int(msg_id)))
        db.commit()
        db.close()
    except Exception as e:
        print(f"db update failed: {e}", flush=True)

    if click_xy:
        x, y = click_xy
        if not ui_click(x, y):
            print("thumbnail click failed", flush=True)
            sys.exit(5)
        time.sleep(2.5)  # 等查看器打开
        full_shot = os.path.join(MEDIA_DIR, f"{msg_id}.png")
        if not shot(full_shot):
            print("viewer screenshot failed", flush=True)
            ui_esc()
            sys.exit(6)
        ui_esc()
        # 回写 DB
        try:
            db = sqlite3.connect(DB_PATH, timeout=10)
            db.execute(
                "UPDATE messages SET media_path=?, needs_media=0, "
                "updated_at=? WHERE id=?",
                (full_shot, int(time.time()), int(msg_id)))
            db.commit()
            db.close()
        except Exception as e:
            print(f"db update failed: {e}", flush=True)
            sys.exit(7)
        print(full_shot, flush=True)


if __name__ == "__main__":
    main()
