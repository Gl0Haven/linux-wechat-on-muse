#!/usr/bin/env python3
"""
事件驱动 worker 的等待器（本地脚本，非 LLM）。

每秒查询 SQLite，出现以下任一条件即退出（退出码 0）并打印触发原因：
  - NEW_MSG: 有 should_reply=1 的 NEW 消息（EXPLICIT，需立即回复）
  - RESEARCH_DONE: 有 research job 变为 DONE/TIMEOUT
  - MUTE_CHANGED: mute 状态变化（与启动时记录的不一致）
  - TIMEOUT: 达到最长等待时间（默认 600 秒，用于心跳）

用法：
  python3 event_waiter.py [--timeout 600] [--mute-state 0/1]

输出（stdout 最后一行）：
  WAKE:<reason>:<detail>
例如：WAKE:NEW_MSG:3（3 条待回复）

设计：由 worker 以后台命令启动，结束时自动唤醒 worker 回合（T1 已验证）。
"""

import sys
import time
import argparse
import sqlite3
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from msgqueue import get_db, is_muted

def check_new_msg(db):
    """是否有待回复的 EXPLICIT 消息。"""
    row = db.execute("""
        SELECT COUNT(*) FROM messages
        WHERE should_reply = 1
          AND status IN ('NEW', 'RETRY')
    """).fetchone()
    return row[0] if row else 0

def check_research_done(db):
    """是否有完成的 research job（步骤2 预留，当前表结构用 research_status）。"""
    try:
        row = db.execute("""
            SELECT COUNT(*) FROM messages
            WHERE research_status IN ('DONE', 'FAILED', 'TIMEOUT')
              AND status = 'WAITING_RESEARCH'
        """).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=600,
                        help="最长等待秒数（默认 600）")
    parser.add_argument("--mute-state", type=str, default="",
                        help="启动时的 mute 状态（0/1），用于检测变化")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="轮询间隔秒数（默认 1.0）")
    args = parser.parse_args()

    start = time.time()
    deadline = start + args.timeout
    initial_muted = args.mute_state == "1" if args.mute_state else None

    db = get_db()
    try:
        while True:
            now = time.time()
            if now >= deadline:
                print(f"WAKE:TIMEOUT:heartbeat", flush=True)
                return 0

            # 1. 新消息
            n = check_new_msg(db)
            if n > 0:
                print(f"WAKE:NEW_MSG:{n}", flush=True)
                return 0

            # 2. research 完成（步骤2）
            r = check_research_done(db)
            if r > 0:
                print(f"WAKE:RESEARCH_DONE:{r}", flush=True)
                return 0

            # 3. mute 状态变化
            if initial_muted is not None:
                muted, _ = is_muted(db, int(now))
                if muted != initial_muted:
                    print(f"WAKE:MUTE_CHANGED:{1 if muted else 0}", flush=True)
                    return 0

            # 短睡，避免忙轮询
            time.sleep(args.interval)
    finally:
        db.close()

if __name__ == "__main__":
    sys.exit(main())
