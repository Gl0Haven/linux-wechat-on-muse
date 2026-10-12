#!/usr/bin/env python3
"""
sync_heartbeat.py - 微信客户端同步心跳

问题背景：微信 Linux 客户端在 Xvfb 下无人值守时，服务器推送通道可能进入
"假活"状态——进程活着、显示已登录、TCP 连接 ESTABLISHED，但新消息推送不下来，
message_db 长时间零写入（2026-10-06 实测 12 分钟）。

解决：定期用 GUI 点击一下主群聊，强制客户端聚焦并触发同步拉取。
只点击，不发送任何消息，不干扰 sendd。

安全：
- 发送中（SENDING）时跳过，不跟 sendd 抢 GUI
- 未登录（login_state != LOGGED_IN）时跳过——登录框上点聊天没意义
- DB 3 分钟内有写入时跳过（同步正常，无需点击）
- 只点[主群]（主群），不乱切
"""
import sys
import time
import subprocess
import os

sys.path.insert(0, '~/workspace/wechat-bridge')
from msgqueue import get_db
from chat_order import get_position

# 主群 ID
MAIN_CHAT = 'YOUR_MAIN_GROUP@chatroom'

def main():
    db = get_db()

    # 1. sendd 正在发送？是就跳过
    sending = db.execute(
        "SELECT COUNT(*) FROM messages WHERE status='SENDING'"
    ).fetchone()[0]
    if sending > 0:
        print(f"[{time.strftime('%H:%M:%S')}] skip: sendd busy ({sending} SENDING)")
        return 0

    # 2. 登录态检查：未登录时点击毫无意义（可能点到登录框），跳过
    #    login_watch 负责上报 LOGIN_REQUIRED
    try:
        import json
        with open('~/workspace/wechat-bridge/state/login_state.json') as f:
            login_state = json.load(f).get('state', 'UNKNOWN')
        if login_state != 'LOGGED_IN':
            print(f"[{time.strftime('%H:%M:%S')}] skip: login state={login_state}, nothing to sync")
            return 0
    except OSError:
        pass  # 状态文件读不到时按旧逻辑继续

    # 3. 检查 DB 新鲜度：如果 3 分钟内有写入，说明同步正常，跳过
    #    （避免无谓点击）
    import glob
    db_files = glob.glob(
        os.path.expanduser('~/xwechat_files/wxid_YOUR_BOT_ACCOUNT/db_storage/message/message_0.db*')
    )
    now = time.time()
    fresh = False
    for f in db_files:
        try:
            if now - os.path.getmtime(f) < 180:
                fresh = True
                break
        except OSError:
            pass
    if fresh:
        print(f"[{time.strftime('%H:%M:%S')}] skip: db fresh (<3min)")
        return 0

    # 3. 点击群聊触发同步（坐标从 chat_order 动态读取，防列表重排点错群）
    pos = get_position(MAIN_CHAT)
    CHAT_X, CHAT_Y = pos["x"], pos["y"]
    try:
        r = subprocess.run(
            ['python3', '~/workspace/wechat-bridge/send-ui.py',
             'click_chat', str(CHAT_X), str(CHAT_Y)],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, 'DISPLAY': ':99'}
        )
        if r.returncode == 0:
            print(f"[{time.strftime('%H:%M:%S')}] heartbeat: clicked chat ({CHAT_X},{CHAT_Y})")
        else:
            print(f"[{time.strftime('%H:%M:%S')}] heartbeat FAILED: {r.stderr.strip()[:100]}")
            return 1
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] heartbeat ERROR: {e}")
        return 1

    return 0

if __name__ == '__main__':
    sys.exit(main())
